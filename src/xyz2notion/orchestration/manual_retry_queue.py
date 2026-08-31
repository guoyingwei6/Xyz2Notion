"""Consume explicit Episode retry requests from one safe, stage-aware queue."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Protocol

from xyz2notion.config import (
    AppConfig,
    AsrProvider,
    ConfigurationError,
    MissingCredentialError,
    load_config,
    load_runtime_credentials,
)
from xyz2notion.enrichment.client import preflight_summary_client
from xyz2notion.enrichment.pipeline import SummaryPolicy
from xyz2notion.notion.client import JsonObject, NotionAPIError, NotionClient
from xyz2notion.notion.initializer import NotionInitializer
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    EpisodeCandidate,
    ProcessingOutcome,
    ai_category_label,
    ai_category_priority,
    build_provider_clients,
    episode_candidates,
)
from xyz2notion.orchestration.state_store import EpisodeAIState, NotionEpisodeStateStore
from xyz2notion.state import PipelineState

MANUAL_RETRY_PROPERTY = "人工请求重试"
MANUAL_RETRY_LIMIT = 2
MANUAL_RETRY_INTER_EPISODE_SECONDS = 60
_MANUAL_REQUEST_INCOMPLETE_ACTIONS = frozenset(
    {"pending", "paused", "waiting_summary_key", "summary_paused"}
)


class ManualRetryStateStore(Protocol):
    """Small protocol-like surface used by the pure selection helper."""

    def load(self, page: Mapping[str, object], eid: str) -> EpisodeAIState: ...


@dataclass(frozen=True)
class ManualRetryItem:
    candidate: EpisodeCandidate
    page: JsonObject
    state: EpisodeAIState

    @property
    def retry_failed(self) -> bool:
        return self.state.record.state is PipelineState.FAILED_RETRYABLE


@dataclass(frozen=True)
class ManualRetryQueueResult:
    """Aggregate-only result safe to print in a GitHub Actions summary."""

    selected: int
    remaining: int
    actions: Mapping[str, int]
    states: Mapping[str, int]
    categories: Mapping[str, int] = field(default_factory=dict)
    failure_categories: Mapping[str, int] = field(default_factory=dict)

    @property
    def has_failures(self) -> bool:
        return self.actions.get("failed", 0) > 0

    def summary(self) -> str:
        def render(values: Mapping[str, int]) -> str:
            return ", ".join(f"{name}={values[name]}" for name in sorted(values)) or "none=0"

        outcome = "FAILED" if self.has_failures else "OK"
        return (
            f"Manual retry queue {outcome} "
            f"(selected={self.selected}; remaining={self.remaining}; "
            f"actions: {render(self.actions)}; states: {render(self.states)}; "
            f"categories: {render(self.categories)}; "
            f"failure_categories: {render(self.failure_categories)})"
        )


def manual_retry_requested(page: Mapping[str, object]) -> bool:
    """Read the single user-facing retry switch without relying on its status."""
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return False
    value = properties.get(MANUAL_RETRY_PROPERTY)
    return bool(value.get("checkbox")) if isinstance(value, Mapping) else False


def manual_reopen_target(state: EpisodeAIState) -> PipelineState | None:
    """Infer the failed stage from persisted transcript/summary checkpoints."""
    if state.record.state is not PipelineState.FAILED_FINAL:
        return None
    if state.summary is not None:
        return PipelineState.ENRICHED
    if state.transcript is not None:
        return PipelineState.TRANSCRIBED
    return PipelineState.DISCOVERED


def _manual_item_sort_key(item: ManualRetryItem) -> tuple[int, int, str]:
    # Category order is the user's explicit preference.  Within a category,
    # resume enrichment/publish before a fresh ASR retry because it has already
    # consumed the earlier ASR checkpoint.
    stage_priority = {
        PipelineState.FAILED_RETRYABLE: 0,
        PipelineState.FAILED_FINAL: 1,
        PipelineState.PUBLISHED: 3,
    }
    return (
        ai_category_priority(item.page),
        stage_priority.get(item.state.record.state, 2),
        item.candidate.page_id,
    )


def select_manual_retry_work(
    pages: Sequence[JsonObject],
    state_store: ManualRetryStateStore,
    *,
    limit: int | None = None,
) -> tuple[ManualRetryItem, ...]:
    """Select checked rows in favorite→liked→heard→listening→to-listen order.

    The checkbox is an explicit priority request, not merely a way to reopen
    failures. Normal checkpoints go through the same processor unchanged;
    failed checkpoints resume from their persisted stage, and published rows
    are selected only so a stale checkbox can be cleared safely.
    """
    found: list[ManualRetryItem] = []
    for page in pages:
        if not manual_retry_requested(page) or not page.get("id"):
            continue
        candidates = episode_candidates(
            [page],
            include_final=True,
            manual_override=True,
        )
        if not candidates:
            continue
        candidate = candidates[0]
        try:
            state = state_store.load(page, candidate.eid)
        except NotionAPIError:
            # A broken state file is not safe to reopen; leave the checkbox for
            # a later audit instead of silently restarting ASR.
            continue
        if state.record.state is PipelineState.FAILED_RETRYABLE:
            if state.record.resume_state is None:
                continue
        elif state.record.state is PipelineState.FAILED_FINAL and state.record.failure is None:
            continue
        found.append(ManualRetryItem(candidate, page, state))
    found.sort(key=_manual_item_sort_key)
    return tuple(found if limit is None else found[: max(0, limit)])


def _summary_policy(config: AppConfig) -> SummaryPolicy:
    return SummaryPolicy(
        prompt_version=config.summary.prompt_version,
        chunk_tokens=config.summary.chunk_tokens,
        chunk_minutes=config.summary.chunk_minutes,
        max_output_tokens=config.summary.max_output_tokens,
    )


def _limit(requested: int | None) -> int:
    if requested is None:
        return MANUAL_RETRY_LIMIT
    if requested < 1:
        raise ValueError("manual retry limit must be positive")
    return min(requested, MANUAL_RETRY_LIMIT)


def run_manual_retry_queue(
    *,
    config_path: str,
    requested_limit: int | None = None,
    page_id_override: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> ManualRetryQueueResult:
    """Process checked rows, reopening failures from the exact stage."""
    config = load_config(config_path)
    if not config.summary.enabled:
        raise ConfigurationError("summary.enabled must be true for manual retries")
    if not config.summary.local_qwen_fallback:
        raise ConfigurationError("summary.local_qwen_fallback must be true for manual retries")
    credentials = load_runtime_credentials()
    credentials.require("notion_token")
    page_id = page_id_override or credentials.notion_page_id
    if not page_id:
        raise MissingCredentialError("Missing target page: set NOTION_PAGE_ID or pass --page-id")
    if credentials.notion_token is None:
        raise AssertionError("credential requirement did not narrow notion_token")

    with ExitStack() as stack:
        notion = stack.enter_context(NotionClient(credentials.notion_token))
        initialization = NotionInitializer(notion, page_id).initialize()
        pages = notion.query_data_source(
            initialization.resources["episode"].data_source_id,
            {"page_size": 100},
        )
        state_store = stack.enter_context(NotionEpisodeStateStore(notion))
        all_items = select_manual_retry_work(pages, state_store)
        selected = all_items[: _limit(requested_limit)]
        if progress is not None:
            progress(
                "Manual retry queue selection "
                f"(selected={len(selected)}; available={len(all_items)})"
            )

        providers = set(config.asr.provider_order)
        dashscope, siliconflow, local_whisper, summary_client = build_provider_clients(
            dashscope_api_key=(
                credentials.dashscope_api_key if AsrProvider.DASHSCOPE in providers else None
            ),
            dashscope_model=config.asr.dashscope_model,
            dashscope_models=config.asr.dashscope_models,
            siliconflow_asr_api_key=(
                credentials.siliconflow_api_key if AsrProvider.SILICONFLOW in providers else None
            ),
            siliconflow_summary_api_key=credentials.siliconflow_api_key,
            siliconflow_asr_models=config.asr.siliconflow_models,
            siliconflow_summary_models=config.summary.siliconflow_models,
            local_whisper_model=(
                config.asr.local_whisper_model if AsrProvider.LOCAL_WHISPER in providers else None
            ),
            local_qwen_summary=config.summary.local_qwen_fallback,
            local_summary_progress=progress,
        )
        for client in (dashscope, siliconflow, local_whisper, summary_client):
            if client is not None:
                stack.enter_context(client)
        if selected and summary_client is not None:
            if progress is not None:
                progress("Manual retry summary preflight started")
            preflight_summary_client(summary_client)
            if progress is not None:
                progress("Manual retry summary preflight OK")

        processor = EpisodeAIProcessor(
            notion,
            state_store,
            dashscope=dashscope,
            siliconflow=siliconflow,
            local_whisper=local_whisper,
            summary_client=summary_client,
            summary_policy=_summary_policy(config),
            summary_enabled=True,
            mindmap_data_source_id=initialization.resources["mindmap"].data_source_id,
        )
        outcomes: list[ProcessingOutcome] = []
        for index, item in enumerate(selected):
            if index:
                time.sleep(MANUAL_RETRY_INTER_EPISODE_SECONDS)
            if progress is not None:
                progress(
                    f"Manual retry item {index + 1}/{len(selected)} started "
                    f"(checkpoint={item.state.record.state.value})"
                )
            page = item.page
            state = item.state
            if state.record.state is PipelineState.FAILED_FINAL:
                target = manual_reopen_target(state)
                if target is None:
                    continue
                state_store.save(
                    item.candidate.page_id,
                    state.model_copy(update={"record": state.record.manual_reopen(target)}),
                )
                # The immutable state file URL changed; reload the page before
                # handing it to the processor so it cannot use the old snapshot.
                page = notion.retrieve_page(item.candidate.page_id)
            outcome = processor.process(
                item.candidate,
                page,
                retry_failed=item.retry_failed,
            )
            # Keep the request while an asynchronous/incomplete checkpoint is
            # still waiting. Once a processing attempt finishes (including a
            # new retryable failure), the ordinary queue owns the next retry.
            if outcome.action not in _MANUAL_REQUEST_INCOMPLETE_ACTIONS:
                state_store.clear_manual_retry(item.candidate.page_id)
            outcomes.append(outcome)
            if progress is not None:
                progress(
                    f"Manual retry item {index + 1}/{len(selected)} finished "
                    f"(action={outcome.action}; state={outcome.state.value})"
                )

    actions = Counter(outcome.action for outcome in outcomes)
    states = Counter(outcome.state.value for outcome in outcomes)
    categories = Counter(ai_category_label(item.page) for item in selected[: len(outcomes)])
    failure_categories = Counter(
        outcome.detail or "unknown" for outcome in outcomes if outcome.action == "failed"
    )
    return ManualRetryQueueResult(
        selected=len(outcomes),
        remaining=max(0, len(all_items) - len(outcomes)),
        actions=actions,
        states=states,
        categories=categories,
        failure_categories=failure_categories,
    )
