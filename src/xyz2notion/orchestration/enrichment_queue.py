"""Consume persisted transcript checkpoints without invoking an ASR provider."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import SecretStr

from xyz2notion.config import (
    AppConfig,
    ConfigurationError,
    MissingCredentialError,
    load_config,
    load_runtime_credentials,
)
from xyz2notion.enrichment.client import (
    FallbackSummaryClient,
    StructuredSummaryClient,
    SummaryPreflightResult,
    preflight_summary_client,
)
from xyz2notion.enrichment.local_qwen import LocalQwenSummaryClient
from xyz2notion.enrichment.pipeline import SummaryPolicy
from xyz2notion.enrichment.siliconflow import SiliconFlowSummaryClient
from xyz2notion.models import ProviderError
from xyz2notion.notion.client import JsonObject, NotionAPIError, NotionClient
from xyz2notion.notion.initializer import NotionInitializer
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    EpisodeCandidate,
    ProcessingOutcome,
    ai_category_label,
    ai_category_priority,
    episode_candidates,
)
from xyz2notion.orchestration.state_store import NotionEpisodeStateStore
from xyz2notion.state import PipelineState

BACKLOG_LIMIT = 2
NORMAL_LIMIT = 2
RETRY_LIMIT = 2
LEGACY_ENRICHMENT_ASR_STATUSES = frozenset({"已转写", "已增强"})
ENRICHMENT_WORK_STATUSES = frozenset({"待增强", "待发布"})
RETRYABLE_STATUS = "可重试失败"
RETRYABLE_ENRICHMENT_STATES = frozenset({PipelineState.TRANSCRIBED, PipelineState.ENRICHED})
_STATUS_PRIORITY = {RETRYABLE_STATUS: 0, "待发布": 1, "待增强": 2}
_LEGACY_STATUS_PRIORITY = {"已增强": 1, "已转写": 2}


class EnrichmentProcessor(Protocol):
    """The processor surface used by the ASR-free queue."""

    def process(
        self,
        candidate: EpisodeCandidate,
        page: Mapping[str, Any],
        *,
        retry_failed: bool = False,
        only_failed: bool = False,
    ) -> ProcessingOutcome: ...


@dataclass(frozen=True)
class EnrichmentQueueResult:
    """Private-safe aggregate result for one bounded queue pass."""

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
        action_summary = (
            ", ".join(f"{name}={self.actions[name]}" for name in sorted(self.actions)) or "none=0"
        )
        state_summary = (
            ", ".join(f"{name}={self.states[name]}" for name in sorted(self.states)) or "none=0"
        )
        category_summary = (
            ", ".join(f"{name}={self.categories[name]}" for name in sorted(self.categories))
            or "none=0"
        )
        failure_summary = (
            ", ".join(
                f"{name}={self.failure_categories[name]}"
                for name in sorted(self.failure_categories)
            )
            or "none=0"
        )
        outcome = "FAILED" if self.has_failures else "OK"
        return (
            f"Transcript enrichment {outcome} "
            f"(selected={self.selected}; remaining={self.remaining}; "
            f"actions: {action_summary}; states: {state_summary}; "
            f"categories: {category_summary}; failure_categories: {failure_summary})"
        )


def _episode_status(page: Mapping[str, object]) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    value = properties.get("ASR Status")
    if not isinstance(value, Mapping):
        return ""
    selected = value.get("select") or value.get("status")
    if not isinstance(selected, Mapping):
        return ""
    return str(selected.get("name") or "")


def _episode_enrichment_status(page: Mapping[str, object]) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    value = properties.get("增强状态")
    if not isinstance(value, Mapping):
        return ""
    selected = value.get("select") or value.get("status")
    if not isinstance(selected, Mapping):
        return ""
    return str(selected.get("name") or "")


def _eligible_for_enrichment(page: Mapping[str, object]) -> bool:
    enrichment_status = _episode_enrichment_status(page)
    if enrichment_status:
        return enrichment_status in ENRICHMENT_WORK_STATUSES
    return _episode_status(page) in LEGACY_ENRICHMENT_ASR_STATUSES


def _work_status_priority(page: Mapping[str, object], retryable_ids: set[str]) -> int:
    if str(page.get("id") or "") in retryable_ids:
        return _STATUS_PRIORITY[RETRYABLE_STATUS]
    enrichment_status = _episode_enrichment_status(page)
    if enrichment_status:
        return _STATUS_PRIORITY.get(enrichment_status, 3)
    return _LEGACY_STATUS_PRIORITY.get(_episode_status(page), 3)


def resolve_queue_limit(mode: str, requested: int | None = None) -> int:
    """Keep backlog, normal, and retry passes deliberately small."""
    maximum = {
        "backlog": BACKLOG_LIMIT,
        "normal": NORMAL_LIMIT,
        "retry": RETRY_LIMIT,
    }.get(mode)
    if maximum is None:
        raise ValueError(f"unknown enrichment queue mode: {mode}")
    if requested is None:
        return maximum
    if requested < 1:
        raise ValueError("enrichment queue limit must be positive")
    return min(requested, maximum)


def select_enrichment_work(
    pages: Sequence[JsonObject],
    *,
    limit: int,
    retryable_page_ids: Sequence[str] = (),
    only_retryable: bool = False,
) -> tuple[tuple[EpisodeCandidate, JsonObject], ...]:
    """Select persisted transcript/publish checkpoints and stage-safe retries."""
    retryable_ids = set(retryable_page_ids)
    eligible = sorted(
        (
            page
            for page in pages
            if (
                str(page.get("id") or "") in retryable_ids
                if only_retryable
                else _eligible_for_enrichment(page) or str(page.get("id") or "") in retryable_ids
            )
        ),
        key=lambda page: (
            ai_category_priority(page),
            _work_status_priority(page, retryable_ids),
            str(page.get("id") or ""),
        ),
    )
    page_by_id = {str(page.get("id")): page for page in eligible if page.get("id")}
    selected: list[tuple[EpisodeCandidate, JsonObject]] = []
    for candidate in episode_candidates(list(eligible)):
        page = page_by_id.get(candidate.page_id)
        if page is None:
            continue
        selected.append((candidate, page))
        if len(selected) >= limit:
            break
    return tuple(selected)


def process_enrichment_pass(
    pages: Sequence[JsonObject],
    processor: EnrichmentProcessor,
    *,
    limit: int,
    retryable_page_ids: Sequence[str] = (),
    only_retryable: bool = False,
) -> EnrichmentQueueResult:
    """Advance a bounded transcript-only queue pass with aggregate-only output."""
    retryable_ids = set(retryable_page_ids)
    all_work = select_enrichment_work(
        pages,
        limit=max(1, len(pages)),
        retryable_page_ids=tuple(retryable_ids),
        only_retryable=only_retryable,
    )
    selected = all_work[:limit]
    outcomes = [
        processor.process(
            candidate,
            page,
            retry_failed=str(page.get("id") or "") in retryable_ids,
        )
        for candidate, page in selected
    ]
    return EnrichmentQueueResult(
        selected=len(selected),
        remaining=max(0, len(all_work) - len(selected)),
        actions=Counter(outcome.action for outcome in outcomes),
        states=Counter(outcome.state.value for outcome in outcomes),
        categories=Counter(ai_category_label(page) for _candidate, page in selected),
        failure_categories=Counter(
            outcome.detail or "unknown" for outcome in outcomes if outcome.action == "failed"
        ),
    )


def _summary_policy(config: AppConfig) -> SummaryPolicy:
    return SummaryPolicy(
        prompt_version=config.summary.prompt_version,
        chunk_tokens=config.summary.chunk_tokens,
        chunk_minutes=config.summary.chunk_minutes,
        max_output_tokens=config.summary.max_output_tokens,
    )


def _summary_client(
    config: AppConfig,
    api_key: SecretStr | None,
) -> StructuredSummaryClient:
    remote = (
        SiliconFlowSummaryClient(
            api_key,
            models=config.summary.siliconflow_models,
        )
        if api_key is not None
        else None
    )
    local = LocalQwenSummaryClient()
    return FallbackSummaryClient(remote, local)


def run_summary_preflight(*, config_path: str) -> SummaryPreflightResult:
    """Validate the active summary route without reading or changing Notion."""
    config = load_config(config_path)
    if not config.summary.enabled:
        raise ConfigurationError("summary.enabled must be true for summary preflight")
    if not config.summary.local_qwen_fallback:
        raise ConfigurationError("summary.local_qwen_fallback must be true for summary preflight")
    credentials = load_runtime_credentials()
    with _summary_client(config, credentials.siliconflow_api_key) as summary_client:
        return preflight_summary_client(summary_client)


def run_enrichment_queue(
    *,
    config_path: str,
    mode: str,
    requested_limit: int | None = None,
    page_id_override: str | None = None,
) -> EnrichmentQueueResult:
    """Run one transcript-only pass using Notion plus summary providers."""
    config = load_config(config_path)
    if not config.summary.enabled:
        raise ConfigurationError("summary.enabled must be true for transcript enrichment")
    if not config.summary.local_qwen_fallback:
        raise ConfigurationError(
            "summary.local_qwen_fallback must be true for the enrichment queue"
        )
    credentials = load_runtime_credentials()
    credentials.require("notion_token")
    page_id = page_id_override or credentials.notion_page_id
    if not page_id:
        raise MissingCredentialError("Missing target page: set NOTION_PAGE_ID or pass --page-id")
    if credentials.notion_token is None:
        raise AssertionError("credential requirement did not narrow notion_token")
    limit = resolve_queue_limit(mode, requested_limit)

    with ExitStack() as stack:
        notion = stack.enter_context(NotionClient(credentials.notion_token))
        initialization = NotionInitializer(notion, page_id).initialize()
        pages = notion.query_data_source(
            initialization.resources["episode"].data_source_id,
            {"page_size": 100},
        )
        summary_client = stack.enter_context(
            _summary_client(config, credentials.siliconflow_api_key)
        )
        state_store = stack.enter_context(NotionEpisodeStateStore(notion))
        retryable_page_ids: set[str] = set()
        for page in pages:
            if _episode_enrichment_status(page) != RETRYABLE_STATUS or not page.get("id"):
                continue
            candidates = episode_candidates([page])
            if not candidates:
                continue
            try:
                state = state_store.load(page, candidates[0].eid)
            except NotionAPIError:
                # A stale/unavailable checkpoint must not block the rest of the
                # bounded queue or turn a retry into an ASR restart.
                continue
            if state.record.resume_state in RETRYABLE_ENRICHMENT_STATES:
                retryable_page_ids.add(str(page["id"]))
        if select_enrichment_work(
            pages,
            limit=1,
            retryable_page_ids=tuple(retryable_page_ids),
            only_retryable=mode == "retry",
        ):
            preflight_summary_client(summary_client)
        processor = EpisodeAIProcessor(
            notion,
            state_store,
            siliconflow=None,
            local_whisper=None,
            summary_client=summary_client,
            summary_policy=_summary_policy(config),
            summary_enabled=True,
            mindmap_data_source_id=initialization.resources["mindmap"].data_source_id,
        )
        return process_enrichment_pass(
            pages,
            processor,
            limit=limit,
            retryable_page_ids=tuple(retryable_page_ids),
            only_retryable=mode == "retry",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich persisted transcripts without invoking ASR",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--page-id")
    parser.add_argument("--mode", choices=("backlog", "normal", "retry"), default="normal")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="test the configured summary route without reading or changing Notion",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.preflight_only:
            preflight = run_summary_preflight(config_path=args.config)
            print(preflight.summary())
            return 0
        result = run_enrichment_queue(
            config_path=args.config,
            mode=args.mode,
            requested_limit=args.limit,
            page_id_override=args.page_id,
        )
    except (ConfigurationError, MissingCredentialError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    except ProviderError as exc:
        failure = exc.failure
        code = f"; code={failure.code}" if failure.code else ""
        print(
            "Summary route FAILED "
            f"(provider={failure.provider}; category={failure.category.value}{code}; "
            f"detail={failure.message})",
            file=sys.stderr,
        )
        return 5
    print(result.summary())
    return 5 if result.has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
