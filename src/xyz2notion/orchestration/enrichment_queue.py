"""Consume persisted transcript checkpoints without invoking an ASR provider."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

from xyz2notion.config import (
    AppConfig,
    ConfigurationError,
    MissingCredentialError,
    load_config,
    load_runtime_credentials,
)
from xyz2notion.enrichment.client import FallbackSummaryClient, StructuredSummaryClient
from xyz2notion.enrichment.local_qwen import LocalQwenSummaryClient
from xyz2notion.enrichment.pipeline import SummaryPolicy
from xyz2notion.enrichment.siliconflow import SiliconFlowSummaryClient
from xyz2notion.notion.client import JsonObject, NotionAPIError, NotionClient
from xyz2notion.notion.initializer import NotionInitializer
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    EpisodeCandidate,
    ProcessingOutcome,
    episode_candidates,
)
from xyz2notion.orchestration.state_store import NotionEpisodeStateStore

BACKLOG_LIMIT = 2
NORMAL_LIMIT = 2
ENRICHMENT_STATUSES = frozenset({"已转写", "已增强"})
_STATUS_PRIORITY = {"已增强": 0, "已转写": 1}


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

    def summary(self) -> str:
        action_summary = (
            ", ".join(f"{name}={self.actions[name]}" for name in sorted(self.actions)) or "none=0"
        )
        state_summary = (
            ", ".join(f"{name}={self.states[name]}" for name in sorted(self.states)) or "none=0"
        )
        return (
            f"Transcript enrichment OK (selected={self.selected}; remaining={self.remaining}; "
            f"actions: {action_summary}; states: {state_summary})"
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


def resolve_queue_limit(mode: str, requested: int | None = None) -> int:
    """Keep backlog and normal passes deliberately small."""
    maximum = BACKLOG_LIMIT if mode == "backlog" else NORMAL_LIMIT
    if requested is None:
        return maximum
    if requested < 1:
        raise ValueError("enrichment queue limit must be positive")
    return min(requested, maximum)


def select_enrichment_work(
    pages: Sequence[JsonObject],
    *,
    limit: int,
) -> tuple[tuple[EpisodeCandidate, JsonObject], ...]:
    """Select only persisted transcript/publish checkpoints, never ASR candidates."""
    eligible = sorted(
        (page for page in pages if _episode_status(page) in ENRICHMENT_STATUSES),
        key=lambda page: (
            _STATUS_PRIORITY[_episode_status(page)],
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
) -> EnrichmentQueueResult:
    """Advance a bounded transcript-only queue pass with aggregate-only output."""
    all_work = select_enrichment_work(pages, limit=max(1, len(pages)))
    selected = all_work[:limit]
    outcomes = [processor.process(candidate, page) for candidate, page in selected]
    return EnrichmentQueueResult(
        selected=len(selected),
        remaining=max(0, len(all_work) - len(selected)),
        actions=Counter(outcome.action for outcome in outcomes),
        states=Counter(outcome.state.value for outcome in outcomes),
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
        return process_enrichment_pass(pages, processor, limit=limit)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich persisted transcripts without invoking ASR",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--page-id")
    parser.add_argument("--mode", choices=("backlog", "normal"), default="normal")
    parser.add_argument("--limit", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
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
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
