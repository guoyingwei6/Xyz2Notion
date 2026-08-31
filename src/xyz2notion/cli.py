"""Command-line interface for Xyz2Notion."""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path

from pydantic import SecretStr

from xyz2notion import __version__
from xyz2notion.config import (
    AsrProvider,
    ConfigurationError,
    MissingCredentialError,
    config_schema_json,
    load_config,
    load_runtime_credentials,
)
from xyz2notion.enrichment.client import SUMMARY_FALLBACK_PROVIDER
from xyz2notion.migration.legacy import LegacyTemplateMigrator
from xyz2notion.migration.schema import (
    CURRENT_WORKSPACE_SCHEMA_VERSION,
    detect_workspace_schema_version,
    migration_plan,
)
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    local_today,
)
from xyz2notion.notion.client import JsonObject, NotionAPIError, NotionClient
from xyz2notion.notion.cover_localizer import NotionCoverLocalizer
from xyz2notion.notion.initializer import DATA_PAGE_TITLE, HOME_MARKER_URL, NotionInitializer
from xyz2notion.notion.published_ai import PublishedAIReconciler
from xyz2notion.orchestration.manual_retry_queue import run_manual_retry_queue
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    ai_category_label,
    ai_category_priority,
    build_provider_clients,
    episode_candidates,
)
from xyz2notion.orchestration.recovery import reset_episode_ai
from xyz2notion.orchestration.state_store import EpisodeAIState, NotionEpisodeStateStore
from xyz2notion.security import CredentialKind, allowed_hosts
from xyz2notion.state import PipelineRecord, PipelineState
from xyz2notion.statistics.incremental import NotionIncrementalStatistics
from xyz2notion.statistics.notion_sync import HeatmapPublisher
from xyz2notion.sync.metadata import MetadataSynchronizer
from xyz2notion.sync.pipeline import collect_metadata
from xyz2notion.xiaoyuzhou.client import XiaoyuzhouAPIError, XiaoyuzhouClient

ASR_INTER_EPISODE_SECONDS = 60
ARCHIVE_INTER_PAGE_SECONDS = 0.4


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="xyz2notion",
        description="Sync Xiaoyuzhou podcasts and enrichments to Notion.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check the local installation")
    config_check = subparsers.add_parser("config-check", help="validate a public YAML config")
    config_check.add_argument("--config", default="config.yaml", help="path to config.yaml")
    subparsers.add_parser("config-schema", help="print the generated JSON Schema")
    notion_init = subparsers.add_parser(
        "notion-init",
        help="create or reconcile the Xyz2Notion workspace",
    )
    notion_init.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    notion_init.add_argument(
        "--create-home",
        action="store_true",
        help="bootstrap homepage blocks once on an otherwise empty target page",
    )
    audit_dashboard = subparsers.add_parser(
        "audit-dashboard",
        help="report aggregate root dashboard block counts without changing Notion",
    )
    audit_dashboard.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    audit_view_configurations = subparsers.add_parser(
        "audit-view-configurations",
        help="report managed Notion view configuration property counts without changing Notion",
    )
    audit_view_configurations.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    audit_view_configurations.add_argument(
        "--details",
        action="store_true",
        help="include configured property names for each managed view",
    )
    cleanup_dashboard = subparsers.add_parser(
        "cleanup-dashboard-layout",
        help="archive an exact set of duplicate managed dashboard layout bundles",
    )
    cleanup_dashboard.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    cleanup_dashboard.add_argument(
        "--confirm",
        required=True,
        help="required destructive-operation confirmation",
    )
    cleanup_dashboard.add_argument(
        "--expected-bundles",
        required=True,
        type=int,
        help="exact expected duplicate six-block layout bundle count",
    )
    cleanup_dashboard.add_argument(
        "--expected-total",
        required=True,
        type=int,
        help="exact expected root block count before cleanup",
    )
    rebuild_dashboard = subparsers.add_parser(
        "rebuild-dashboard",
        help="archive an exact set of root linked database blocks and rebuild views",
    )
    rebuild_dashboard.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    rebuild_dashboard.add_argument(
        "--confirm",
        required=True,
        help="required destructive-operation confirmation",
    )
    rebuild_dashboard.add_argument(
        "--expected-count",
        required=True,
        type=int,
        help="exact expected root child_database block count",
    )
    rebuild_layout = subparsers.add_parser(
        "rebuild-dashboard-layout",
        help="replace one exact managed root dashboard layout without touching its data page",
    )
    rebuild_layout.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    rebuild_layout.add_argument(
        "--confirm",
        required=True,
        help="required destructive-operation confirmation",
    )
    rebuild_layout.add_argument(
        "--expected-total",
        required=True,
        type=int,
        help="exact expected root block count before replacement",
    )
    subparsers.add_parser(
        "xiaoyuzhou-check",
        help="verify Xiaoyuzhou authentication without printing account data",
    )
    sync_metadata = subparsers.add_parser(
        "sync-metadata",
        help="sync Xiaoyuzhou metadata and listening progress to Notion",
    )
    sync_metadata.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    process_asr = subparsers.add_parser(
        "process-asr",
        help="advance only ASR checkpoints and stop at a persisted transcript",
    )
    process_asr.add_argument("--config", default="config.yaml", help="path to config.yaml")
    process_asr.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    process_asr.add_argument(
        "--limit",
        type=int,
        choices=(1, 2),
        default=1,
        help="cap this rate-limited run at one or two Episode candidates",
    )
    process_asr.add_argument(
        "--mode",
        choices=("backlog", "incremental", "retry"),
        default="incremental",
        help="label this run as backlog, normal increment, or retry-only",
    )
    manual_retry = subparsers.add_parser(
        "process-manual-retries",
        help="process checked Episode retries from the persisted failed stage",
    )
    manual_retry.add_argument("--config", default="config.yaml", help="path to config.yaml")
    manual_retry.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    manual_retry.add_argument(
        "--limit",
        type=int,
        choices=(1, 2),
        default=2,
        help="cap this manual-first run at one or two Episode candidates",
    )
    repair_covers = subparsers.add_parser(
        "repair-notion-covers",
        help="upload a bounded number of existing external covers into Notion",
    )
    repair_covers.add_argument("--limit", type=int, default=10)
    repair_covers.add_argument("--confirm", required=True)
    repair_covers.add_argument("--page-id")
    reconcile_ai = subparsers.add_parser(
        "reconcile-published-ai",
        help="audit up to two published Episode pages and backfill mind-map rows",
    )
    reconcile_ai.add_argument("--limit", type=int, default=2)
    reconcile_ai.add_argument("--confirm", required=True)
    reconcile_ai.add_argument("--page-id")
    audit_backlog = subparsers.add_parser(
        "audit-notion-backlog",
        help="report aggregate AI, cover, and zero-play backlog counts without changes",
    )
    audit_backlog.add_argument("--page-id")
    archive_legacy = subparsers.add_parser(
        "archive-legacy-zero-play",
        help="trash an exact, confirmed set of unprotected zero-play Episode pages",
    )
    archive_legacy.add_argument(
        "--expected-count",
        required=True,
        type=int,
        help="exact number of eligible legacy zero-play Episode pages",
    )
    archive_legacy.add_argument(
        "--confirm",
        required=True,
        help="required confirmation bound to the exact expected count",
    )
    archive_legacy.add_argument("--page-id")
    reopen_timeline = subparsers.add_parser(
        "reopen-timeline-failures",
        help="resume bounded timeline-only summary failures from persisted transcripts",
    )
    reopen_timeline.add_argument("--limit", type=int, default=4)
    reopen_timeline.add_argument("--confirm", required=True)
    reopen_timeline.add_argument("--page-id")
    reopen_summary = subparsers.add_parser(
        "reopen-summary-failures",
        help="resume bounded summary failures from persisted transcripts",
    )
    reopen_summary.add_argument("--limit", type=int, default=1)
    reopen_summary.add_argument("--confirm", required=True)
    reopen_summary.add_argument("--page-id")
    migrate = subparsers.add_parser(
        "migrate",
        help="adopt and map a legacy Podcast2Notion template in place",
    )
    migrate.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="report planned counts without changing Notion",
    )
    redo = subparsers.add_parser(
        "redo-episode",
        help="reset one exact Episode AI state without deleting page content",
    )
    redo.add_argument("--eid", required=True, help="exact Xiaoyuzhou Episode ID")
    redo.add_argument(
        "--page-id",
        help="target root page ID; defaults to NOTION_PAGE_ID",
    )
    for name, help_text in (
        ("rebuild-statistics", "recalculate all listening statistics"),
        ("rebuild-heatmap", "rebuild the current-year listening heatmap"),
    ):
        rebuild = subparsers.add_parser(name, help=help_text)
        rebuild.add_argument(
            "--page-id",
            help="target root page ID; defaults to NOTION_PAGE_ID",
        )
    return parser


def _episode_asr_status(page: Mapping[str, object]) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    status_property = properties.get("ASR Status")
    if not isinstance(status_property, Mapping):
        return ""
    selected = status_property.get("select") or status_property.get("status")
    if not isinstance(selected, Mapping):
        return ""
    return str(selected.get("name") or "")


def _episode_enrichment_status(page: Mapping[str, object]) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    status_property = properties.get("增强状态")
    if not isinstance(status_property, Mapping):
        return ""
    selected = status_property.get("select") or status_property.get("status")
    if not isinstance(selected, Mapping):
        return ""
    return str(selected.get("name") or "")


def _eligible_ai_pages(
    pages: Sequence[JsonObject],
    *,
    retry_failed: bool,
) -> list[JsonObject]:
    return [
        page
        for page in pages
        if (
            _episode_asr_status(page) == "可重试失败"
            or _episode_enrichment_status(page) == "可重试失败"
        )
        is retry_failed
    ]


def _ai_page_priority(page: Mapping[str, object]) -> tuple[int, int, str]:
    """Order ASR candidates by category, then persisted checkpoint."""
    checkpoint_priority = {
        "已增强": 0,
        "已转写": 1,
        "转写中": 2,
        "排队中": 3,
        "待处理": 4,
    }.get(_episode_asr_status(page), 5)
    return (ai_category_priority(page), checkpoint_priority, str(page.get("id") or ""))


def _asr_queue_pages[AIPage: Mapping[str, object]](
    pages: Sequence[AIPage],
) -> list[AIPage]:
    """Select only rows that may advance toward, but never beyond, TRANSCRIBED."""
    allowed = {"待处理", "排队中", "转写中"}
    return [page for page in pages if _episode_asr_status(page) in allowed]


def _retryable_asr_page_ids(
    pages: Sequence[JsonObject],
    state_store: NotionEpisodeStateStore,
) -> set[str]:
    """Return only retryable rows whose checkpoint failed during ASR."""
    asr_states = {
        PipelineState.DISCOVERED,
        PipelineState.ASR_SUBMITTED,
        PipelineState.ASR_RUNNING,
    }
    result: set[str] = set()
    for page in pages:
        page_id = str(page.get("id") or "")
        if not page_id or _episode_asr_status(page) != "可重试失败":
            continue
        candidates = episode_candidates([page])
        if not candidates:
            continue
        try:
            state = state_store.load(page, candidates[0].eid)
        except NotionAPIError:
            continue
        if state.record.resume_state in asr_states:
            result.add(page_id)
    return result


def _run_asr_queue(args: argparse.Namespace) -> int:
    """Advance a tightly bounded ASR-only queue without summary or publishing."""
    try:
        config = load_config(args.config)
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")

        selected_providers = set(config.asr.provider_order)
        dashscope_api_key = (
            credentials.dashscope_api_key if AsrProvider.DASHSCOPE in selected_providers else None
        )
        siliconflow_asr_api_key = (
            credentials.siliconflow_api_key
            if AsrProvider.SILICONFLOW in selected_providers
            else None
        )
        local_whisper_model = (
            config.asr.local_whisper_model
            if AsrProvider.LOCAL_WHISPER in selected_providers
            else None
        )

        with ExitStack() as stack:
            notion = stack.enter_context(NotionClient(credentials.notion_token))
            initialization = NotionInitializer(notion, page_id).initialize()
            pages = notion.query_data_source(
                initialization.resources["episode"].data_source_id,
                {"page_size": 100},
            )
            state_store = stack.enter_context(NotionEpisodeStateStore(notion))
            retryable_asr_ids = _retryable_asr_page_ids(pages, state_store)
            if args.mode == "retry":
                eligible_pages = sorted(
                    [page for page in pages if str(page.get("id") or "") in retryable_asr_ids],
                    key=_ai_page_priority,
                )
            else:
                eligible_pages = sorted(
                    [
                        *(_asr_queue_pages(pages)),
                        *[page for page in pages if str(page.get("id") or "") in retryable_asr_ids],
                    ],
                    key=_ai_page_priority,
                )
            all_candidates = episode_candidates(eligible_pages)
            candidates = all_candidates[: args.limit]
            dashscope, siliconflow, local_whisper, _summary_client = build_provider_clients(
                dashscope_api_key=dashscope_api_key,
                dashscope_model=config.asr.dashscope_model,
                dashscope_models=config.asr.dashscope_models,
                siliconflow_asr_api_key=siliconflow_asr_api_key,
                siliconflow_summary_api_key=None,
                siliconflow_asr_models=config.asr.siliconflow_models,
                siliconflow_summary_models=(),
                local_whisper_model=local_whisper_model,
                local_qwen_summary=False,
            )
            for client in (dashscope, siliconflow, local_whisper):
                if client is not None:
                    stack.enter_context(client)
            processor = EpisodeAIProcessor(
                notion,
                state_store,
                dashscope=dashscope,
                siliconflow=siliconflow,
                local_whisper=local_whisper,
                summary_enabled=False,
            )
            page_by_id = {str(page.get("id")): page for page in pages}
            outcomes = []
            for index, candidate in enumerate(candidates):
                if index:
                    time.sleep(ASR_INTER_EPISODE_SECONDS)
                outcomes.append(
                    processor.process_asr_only(
                        candidate,
                        page_by_id[candidate.page_id],
                        retry_failed=candidate.page_id in retryable_asr_ids,
                    )
                )
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    actions = Counter(outcome.action for outcome in outcomes)
    states = Counter(outcome.state.value for outcome in outcomes)
    providers = Counter(outcome.detail or "unknown" for outcome in outcomes)
    action_summary = ", ".join(f"{name}={actions[name]}" for name in sorted(actions)) or "none=0"
    state_summary = ", ".join(f"{name}={states[name]}" for name in sorted(states)) or "none=0"
    provider_summary = (
        ", ".join(f"{name}={providers[name]}" for name in sorted(providers)) or "none=0"
    )
    category_counts = Counter(
        ai_category_label(page_by_id[candidate.page_id]) for candidate in candidates
    )
    category_summary = (
        ", ".join(f"{name}={category_counts[name]}" for name in sorted(category_counts)) or "none=0"
    )
    remaining = max(0, len(all_candidates) - len(candidates)) + sum(
        outcome.state in {PipelineState.ASR_SUBMITTED, PipelineState.ASR_RUNNING}
        for outcome in outcomes
    )
    print(
        f"Episode ASR queue OK (mode={args.mode}; selected={len(candidates)}; "
        f"remaining={remaining}; interval_seconds={ASR_INTER_EPISODE_SECONDS}; "
        f"actions: {action_summary}; states: {state_summary}; categories: {category_summary}; "
        f"providers: {provider_summary})"
    )
    return 0


def _run_manual_retry_queue(args: argparse.Namespace) -> int:
    """Run the user-requested, stage-aware retry queue with aggregate output."""
    try:
        config_path = args.config
        if config_path == "config.yaml" and not Path(config_path).is_file():
            config_path = "config.example.yaml"
        result = run_manual_retry_queue(
            config_path=config_path,
            requested_limit=args.limit,
            page_id_override=args.page_id,
            progress=lambda message: print(message, flush=True),
        )
    except (ConfigurationError, MissingCredentialError) as exc:
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


def _notion_runtime(args: argparse.Namespace) -> tuple[SecretStr, str]:
    credentials = load_runtime_credentials()
    credentials.require("notion_token")
    page_id = args.page_id or credentials.notion_page_id
    if not page_id:
        raise MissingCredentialError("Missing target page: set NOTION_PAGE_ID or pass --page-id")
    if credentials.notion_token is None:
        raise AssertionError("credential requirement did not narrow notion_token")
    return credentials.notion_token, page_id


def _run_cover_repair(args: argparse.Namespace) -> int:
    expected = f"REPAIR_{args.limit}_NOTION_COVERS"
    if not 1 <= args.limit <= 10 or args.confirm != expected:
        print(
            f"Confirmation error: use --limit 1..10 and --confirm {expected}",
            file=sys.stderr,
        )
        return 2
    try:
        token, page_id = _notion_runtime(args)
        with NotionClient(token) as notion:
            initialization = NotionInitializer(notion, page_id).initialize()
            with NotionCoverLocalizer(
                notion,
                (initialization.resources["podcast"].data_source_id,),
                sort_property="Total Listening Seconds",
            ) as localizer:
                report = localizer.repair(limit=args.limit)
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    print(
        "Notion cover repair OK "
        f"(limit={args.limit}; repaired={report.repaired}; "
        f"skipped={report.skipped}; failed={report.failed})"
    )
    return 0


def _run_published_ai_reconciliation(args: argparse.Namespace) -> int:
    expected = f"RECONCILE_{args.limit}_PUBLISHED_AI"
    if not 1 <= args.limit <= 2 or args.confirm != expected:
        print(
            f"Confirmation error: use --limit 1..2 and --confirm {expected}",
            file=sys.stderr,
        )
        return 2
    try:
        token, page_id = _notion_runtime(args)
        with NotionClient(token) as notion:
            initialization = NotionInitializer(notion, page_id).initialize()
            pages = notion.query_data_source_page(
                initialization.resources["episode"].data_source_id,
                {
                    "page_size": args.limit,
                    "filter": {
                        "and": [
                            {
                                "property": "ASR Status",
                                "select": {"equals": "已发布"},
                            },
                            {
                                "or": [
                                    {
                                        "property": "转写完成时间",
                                        "date": {"is_empty": True},
                                    },
                                    {
                                        "property": "总结完成时间",
                                        "date": {"is_empty": True},
                                    },
                                    {
                                        "property": "增强 Provider",
                                        "rich_text": {"is_empty": True},
                                    },
                                    {
                                        "property": "增强状态",
                                        "select": {"is_empty": True},
                                    },
                                ]
                            },
                        ]
                    },
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                },
            )
            with NotionEpisodeStateStore(notion) as state_store:
                report = PublishedAIReconciler(
                    notion,
                    state_store,
                    initialization.resources["mindmap"].data_source_id,
                ).reconcile(pages, limit=args.limit)
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    print(
        "Published AI reconciliation OK "
        f"(selected={report.selected}; transcripts={report.transcripts}; "
        f"summaries={report.summaries}; page_ready={report.page_ready}; "
        f"mindmaps_created={report.mindmaps_created}; "
        f"mindmaps_updated={report.mindmaps_updated}; "
        f"mindmaps_unchanged={report.mindmaps_unchanged}; "
        f"incomplete={report.incomplete})"
    )
    return 0


def _notion_property_text(properties: Mapping[str, object], name: str) -> str:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return ""
    items = value.get("rich_text") or value.get("title")
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        plain_text = item.get("plain_text")
        if isinstance(plain_text, str):
            parts.append(plain_text)
            continue
        text = item.get("text")
        if isinstance(text, Mapping) and isinstance(text.get("content"), str):
            parts.append(str(text["content"]))
    return "".join(parts)


def _notion_property_number(properties: Mapping[str, object], name: str) -> float:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return 0
    number = value.get("number")
    return float(number) if isinstance(number, int | float) else 0


def _notion_property_checkbox(properties: Mapping[str, object], name: str) -> bool:
    value = properties.get(name)
    return bool(value.get("checkbox")) if isinstance(value, Mapping) else False


def _legacy_zero_play_pages(
    pages: Sequence[JsonObject],
) -> tuple[list[JsonObject], int, int]:
    """Return eligible legacy pages plus aggregate zero-play protection counts.

    The legacy category is intentionally defined by the same safeguards used by
    ``audit-notion-backlog``: no playback, no playlist/favorite/like marker, and
    no non-pending AI checkpoint.  It is not inferred from a title or an EID.
    """
    candidates: list[JsonObject] = []
    zero_play_total = protected_zero_play = 0
    for page in pages:
        if page.get("in_trash") is True or page.get("is_archived") is True:
            continue
        properties = page.get("properties")
        if not isinstance(properties, Mapping):
            continue
        if _notion_property_number(properties, "Played Seconds") > 0:
            continue
        zero_play_total += 1
        status = _episode_asr_status(page)
        protected = (
            _notion_property_checkbox(properties, "In Playlist")
            or _notion_property_checkbox(properties, "Favorited")
            or _notion_property_checkbox(properties, "Liked")
            or status not in {"", "待处理"}
        )
        if protected:
            protected_zero_play += 1
        else:
            candidates.append(page)
    return candidates, protected_zero_play, zero_play_total


def _cover_storage_kind(properties: Mapping[str, object]) -> str:
    value = properties.get("Cover")
    if not isinstance(value, Mapping):
        return "missing"
    files = value.get("files")
    if not isinstance(files, list) or not files or not isinstance(files[0], Mapping):
        return "missing"
    return "external" if files[0].get("type") == "external" else "notion"


def _safe_failure_reason_code(failure: ProviderFailure) -> str:
    """Reduce known static failures to non-identifying aggregate reason codes."""
    if failure.code in {
        "summary_schema",
        "timeline_constraints",
        "normalization_constraints",
        "completion_schema",
    }:
        return failure.code
    if (
        failure.provider == SUMMARY_FALLBACK_PROVIDER
        and failure.category is ProviderErrorCategory.SCHEMA_CHANGED
        and "fallback=local_qwen_summary:schema_changed" in failure.message
    ):
        return "legacy_local_schema"
    if failure.message in {
        "SiliconFlow JSON repair did not satisfy the summary schema",
        "Local Qwen JSON repair did not satisfy the summary schema",
    }:
        return "summary_schema"
    if failure.message in {
        "SiliconFlow JSON repair did not satisfy timeline constraints",
        "Local Qwen JSON repair did not satisfy timeline constraints",
    }:
        return "timeline_constraints"
    if failure.message == "Local enrichment normalization did not satisfy constraints":
        return "normalization_constraints"
    if failure.message == "Local Qwen returned an unexpected completion schema":
        return "completion_schema"
    if failure.message == "Transcript contains no readable content":
        return "empty_transcript"
    if failure.message == "SiliconFlow rejected the summary request (HTTP 400)":
        code = failure.code or "unknown"
        if len(code) <= 64 and all(character.isalnum() or character in "._-" for character in code):
            return f"request_http_400_{code}"
        return "request_http_400"
    return failure.category.value


def _summary_recovery_priority(reason: str) -> int:
    """Try schema canaries before bulk runtime failures while preserving order."""
    return {
        "legacy_local_schema": 0,
        "summary_schema": 1,
        "completion_schema": 2,
        "timeline_constraints": 3,
        "normalization_constraints": 4,
        "request_http_400_20015": 5,
        "unavailable": 10,
    }.get(reason, 20)


def _run_notion_backlog_audit(args: argparse.Namespace) -> int:
    """Read aggregate cleanup and AI status without exposing Episode identity."""
    try:
        token, page_id = _notion_runtime(args)
        with NotionClient(token) as notion:
            resources = NotionInitializer(notion, page_id).discover_existing_resources()
            episode_resource = resources.get("episode")
            podcast_resource = resources.get("podcast")
            all_resource = resources.get("all")
            if episode_resource is None or podcast_resource is None:
                raise NotionAPIError("Required Xyz2Notion databases were not found")
            episodes = notion.query_data_source(episode_resource.data_source_id)
            podcasts = notion.query_data_source(podcast_resource.data_source_id)
            total_seconds = 0
            baseline_version_set = False
            if all_resource is not None:
                totals = notion.query_data_source(all_resource.data_source_id)
                for page in totals:
                    properties = page.get("properties")
                    if not isinstance(properties, Mapping):
                        continue
                    if _notion_property_text(properties, "Period Key") != "all":
                        continue
                    total_seconds = int(
                        _notion_property_number(properties, "Exact Listening Seconds")
                    )
                    baseline_version_set = bool(
                        _notion_property_text(properties, "Statistics Baseline Version")
                    )
                    break

            normal_candidates = episode_candidates(_eligible_ai_pages(episodes, retry_failed=False))
            retry_candidates = episode_candidates(_eligible_ai_pages(episodes, retry_failed=True))
            asr_statuses = Counter(_episode_asr_status(page) or "未设置" for page in episodes)
            enrichment_statuses = Counter(
                _episode_enrichment_status(page) or "未设置" for page in episodes
            )
            asr_providers: Counter[str] = Counter()
            asr_models: Counter[str] = Counter()
            for page in episodes:
                properties = page.get("properties")
                if not isinstance(properties, Mapping):
                    continue
                provider = _notion_property_text(properties, "ASR Provider")
                model = _notion_property_text(properties, "ASR Model")
                if provider:
                    asr_providers[provider] += 1
                if model:
                    asr_models[model] += 1
            final_pages = [
                page
                for page in episodes
                if _episode_asr_status(page) == "最终失败"
                or _episode_enrichment_status(page) == "最终失败"
            ]
            failure_categories: Counter[str] = Counter()
            with NotionEpisodeStateStore(notion) as state_store:
                for page in final_pages:
                    properties = page.get("properties")
                    if not isinstance(properties, Mapping):
                        failure_categories["state_unreadable"] += 1
                        continue
                    eid = _notion_property_text(properties, "EID")
                    if not eid:
                        failure_categories["state_unreadable"] += 1
                        continue
                    try:
                        state = state_store.load(page, eid)
                    except NotionAPIError:
                        failure_categories["state_unreadable"] += 1
                        continue
                    failure = state.record.failure
                    if failure is None:
                        failure_categories["state_missing_failure"] += 1
                    else:
                        reason = _safe_failure_reason_code(failure)
                        failure_categories[f"{failure.provider}:{reason}"] += 1

            legacy_pages, protected_zero_play, zero_play_total = _legacy_zero_play_pages(episodes)
            legacy_zero_play = len(legacy_pages)

            cover_kinds = Counter(
                _cover_storage_kind(properties)
                for page in podcasts
                if isinstance((properties := page.get("properties")), Mapping)
            )
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    asr_status_summary = (
        ", ".join(f"{name}={asr_statuses[name]}" for name in sorted(asr_statuses)) or "none=0"
    )
    enrichment_status_summary = (
        ", ".join(f"{name}={enrichment_statuses[name]}" for name in sorted(enrichment_statuses))
        or "none=0"
    )
    failure_summary = (
        ", ".join(f"{name}={failure_categories[name]}" for name in sorted(failure_categories))
        or "none=0"
    )
    provider_summary = (
        ", ".join(f"{name}={asr_providers[name]}" for name in sorted(asr_providers)) or "none=0"
    )
    model_summary = (
        ", ".join(f"{name}={asr_models[name]}" for name in sorted(asr_models)) or "none=0"
    )
    print(
        "Notion backlog audit OK "
        f"(episodes={len(episodes)}; normal_ai_candidates={len(normal_candidates)}; "
        f"retry_ai_candidates={len(retry_candidates)}; asr_statuses: {asr_status_summary}; "
        f"enrichment_statuses: {enrichment_status_summary}; "
        f"statistics_total_seconds={total_seconds}; "
        f"statistics_baseline={'set' if baseline_version_set else 'unset'}; "
        f"asr_providers: {provider_summary}; asr_models: {model_summary}; "
        f"final_failure_categories: {failure_summary}; "
        f"zero_play_total={zero_play_total}; "
        f"zero_play_protected={protected_zero_play}; "
        f"legacy_zero_play={legacy_zero_play}; "
        f"podcasts={len(podcasts)}; external_covers={cover_kinds['external']}; "
        f"notion_covers={cover_kinds['notion']}; missing_covers={cover_kinds['missing']})"
    )
    return 0


def _run_archive_legacy_zero_play(args: argparse.Namespace) -> int:
    """Trash exactly the user-confirmed legacy zero-play Episode pages."""
    expected_confirmation = f"ARCHIVE_{args.expected_count}_LEGACY_ZERO_PLAY_EPISODES"
    if args.expected_count < 1 or args.confirm != expected_confirmation:
        print(
            f"Confirmation error: use --expected-count 1.. and --confirm {expected_confirmation}",
            file=sys.stderr,
        )
        return 2

    archived = 0
    try:
        token, page_id = _notion_runtime(args)
        with NotionClient(token) as notion:
            resources = NotionInitializer(notion, page_id).discover_existing_resources()
            episode_resource = resources.get("episode")
            if episode_resource is None:
                raise NotionAPIError("Required Episode database was not found")
            episodes = notion.query_data_source(episode_resource.data_source_id)
            candidates, protected_zero_play, _zero_play_total = _legacy_zero_play_pages(episodes)
            if len(candidates) != args.expected_count:
                print(
                    "Archive refused: exact preflight count changed "
                    f"(expected={args.expected_count}; eligible={len(candidates)}; "
                    f"protected={protected_zero_play}; no changes)",
                    file=sys.stderr,
                )
                return 2
            missing_ids = sum(not page.get("id") for page in candidates)
            if missing_ids:
                print(
                    "Archive refused: eligible pages missing stable IDs "
                    f"(count={missing_ids}; no changes)",
                    file=sys.stderr,
                )
                return 2
            for candidate in candidates:
                notion.update_page(str(candidate["id"]), {"in_trash": True})
                archived += 1
                if archived < len(candidates):
                    time.sleep(ARCHIVE_INTER_PAGE_SECONDS)
            remaining_pages, _remaining_protected, _remaining_zero_play = _legacy_zero_play_pages(
                notion.query_data_source(episode_resource.data_source_id)
            )
            if remaining_pages:
                print(
                    "Archive incomplete: active eligible pages remain "
                    f"(archived={archived}; remaining={len(remaining_pages)})",
                    file=sys.stderr,
                )
                return 4
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(
            f"Notion archive error: {exc} (archived_before_failure={archived})",
            file=sys.stderr,
        )
        return 4

    print(
        "Legacy zero-play archive OK "
        f"(selected={args.expected_count}; archived={archived}; "
        f"protected_zero_play={protected_zero_play}; remaining=0)"
    )
    return 0


def _run_reopen_summary_failures(
    args: argparse.Namespace,
    *,
    allowed_providers: frozenset[str],
    allowed_reasons: frozenset[str],
    confirmation_label: str,
) -> int:
    expected = f"REOPEN_{args.limit}_{confirmation_label}_FAILURES"
    if not 1 <= args.limit <= 10 or args.confirm != expected:
        print(
            f"Confirmation error: use --limit 1..10 and --confirm {expected}",
            file=sys.stderr,
        )
        return 2
    try:
        token, page_id = _notion_runtime(args)
        with NotionClient(token) as notion:
            resources = NotionInitializer(notion, page_id).discover_existing_resources()
            episode_resource = resources.get("episode")
            if episode_resource is None:
                raise NotionAPIError("Required Episode database was not found")
            pages = notion.query_data_source(
                episode_resource.data_source_id,
                {
                    "page_size": 100,
                    "filter": {
                        "or": [
                            {
                                "property": "ASR Status",
                                "select": {"equals": "最终失败"},
                            },
                            {
                                "property": "增强状态",
                                "select": {"equals": "最终失败"},
                            },
                        ]
                    },
                },
            )
            reopened = skipped = 0
            candidates: list[tuple[int, JsonObject, EpisodeAIState]] = []
            with NotionEpisodeStateStore(notion) as state_store:
                for page in pages:
                    properties = page.get("properties")
                    if not isinstance(properties, Mapping) or not page.get("id"):
                        skipped += 1
                        continue
                    eid = _notion_property_text(properties, "EID")
                    if not eid:
                        skipped += 1
                        continue
                    state = state_store.load(page, eid)
                    failure = state.record.failure
                    reason = _safe_failure_reason_code(failure) if failure is not None else ""
                    if (
                        state.record.state is not PipelineState.FAILED_FINAL
                        or failure is None
                        or failure.provider not in allowed_providers
                        or reason not in allowed_reasons
                        or state.transcript is None
                        or state.summary is not None
                    ):
                        skipped += 1
                        continue
                    candidates.append((_summary_recovery_priority(reason), page, state))
                for _priority, page, state in sorted(candidates, key=lambda item: item[0]):
                    if reopened >= args.limit:
                        break
                    eid = state.record.eid
                    record = PipelineRecord(
                        eid=eid,
                        state=PipelineState.TRANSCRIBED,
                        attempts=state.record.attempts,
                        history=state.record.history,
                    )
                    state_store.save(
                        str(page["id"]),
                        state.model_copy(update={"record": record}),
                    )
                    notion.update_page(
                        str(page["id"]),
                        {"properties": {"人工请求重试": {"checkbox": True}}},
                    )
                    reopened += 1
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    print(
        f"Summary failure recovery OK (limit={args.limit}; reopened={reopened}; skipped={skipped})"
    )
    return 0


def _run_reopen_timeline_failures(args: argparse.Namespace) -> int:
    return _run_reopen_summary_failures(
        args,
        allowed_providers=frozenset({"siliconflow_summary"}),
        allowed_reasons=frozenset({"timeline_constraints"}),
        confirmation_label="TIMELINE",
    )


def _run_reopen_all_summary_failures(args: argparse.Namespace) -> int:
    return _run_reopen_summary_failures(
        args,
        allowed_providers=frozenset(
            {
                "siliconflow_summary",
                "local_qwen_summary",
                SUMMARY_FALLBACK_PROVIDER,
            }
        ),
        allowed_reasons=frozenset(
            {
                "summary_schema",
                "timeline_constraints",
                "normalization_constraints",
                "completion_schema",
                "legacy_local_schema",
                "request_http_400_20015",
                "unavailable",
            }
        ),
        confirmation_label="SUMMARY",
    )


def _run_migration(args: argparse.Namespace) -> int:
    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")
        with NotionClient(credentials.notion_token) as notion:
            previous_schema_version = detect_workspace_schema_version(
                notion.list_block_children(page_id)
            )
            migration_plan(previous_schema_version)
            initializer = NotionInitializer(notion, page_id)
            resources = (
                initializer.discover_existing_resources()
                if args.dry_run
                else initializer.initialize().resources
            )
            report = LegacyTemplateMigrator(
                notion,
                resources,
                page_id,
            ).migrate(dry_run=args.dry_run)
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 5
    if report.duplicate_keys:
        print(
            "Migration stopped: duplicate stable keys must be resolved "
            f"(count={len(report.duplicate_keys)})",
            file=sys.stderr,
        )
        return 5
    print(
        f"Migration {'dry-run' if report.dry_run else 'complete'} "
        f"(scanned={report.scanned_pages}, planned={report.planned_updates}, "
        f"updated={report.updated_pages}, legacy embeds found={report.legacy_embeds_found}, "
        f"removed={report.legacy_embeds_removed}, "
        f"schema={previous_schema_version}->{CURRENT_WORKSPACE_SCHEMA_VERSION})"
    )
    return 0


def _run_redo_episode(args: argparse.Namespace) -> int:
    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")
        with NotionClient(credentials.notion_token) as notion:
            initialization = NotionInitializer(notion, page_id).initialize()
            reset_episode_ai(
                notion,
                initialization.resources["episode"].data_source_id,
                args.eid,
            )
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"Recovery error: {exc}", file=sys.stderr)
        return 6
    print("Episode AI state reset OK (count=1)")
    return 0


def _run_rebuild(args: argparse.Namespace, *, heatmap_only: bool) -> int:
    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")
        with NotionClient(credentials.notion_token) as notion:
            initialization = NotionInitializer(notion, page_id).initialize()
            report = NotionIncrementalStatistics(
                notion,
                initialization.resources,
                root_page_id=page_id,
            ).sync()
            heatmap_action = "baseline_preserved"
            if report.mode == "incremental":
                heatmap_action = (
                    HeatmapPublisher(notion, page_id)
                    .publish(
                        local_today().year,
                        report.daily,
                    )
                    .action
                )
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"Statistics error: {exc}", file=sys.stderr)
        return 6
    operation = "heatmap" if heatmap_only else "statistics"
    print(
        f"Notion-only {operation} reconciliation OK "
        f"(mode={report.mode}; baseline_episodes={report.baseline_episodes}; "
        f"ledger_episodes={report.ledger_episodes}; delta_seconds={report.delta_seconds}; "
        f"total_seconds={report.total_seconds}; heatmap={heatmap_action})"
    )
    return 0


def _is_dashboard_marker(block: Mapping[str, object]) -> bool:
    block_type = block.get("type")
    body = block.get(block_type) if isinstance(block_type, str) else None
    if not isinstance(body, Mapping):
        return False
    items = body.get("rich_text")
    if not isinstance(items, Sequence):
        return False
    for item in items:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        link = text.get("link") if isinstance(text, Mapping) else None
        if isinstance(link, Mapping) and link.get("url") == HOME_MARKER_URL:
            return True
    return False


_MANAGED_LAYOUT_BUNDLE_SHAPE = (
    "heading_1",
    "callout",
    "column_list",
    "divider",
    "callout",
    "paragraph",
)


def _layout_bundle_ranges(blocks: Sequence[Mapping[str, object]]) -> list[range]:
    block_types = [str(block.get("type", "")) for block in blocks]
    width = len(_MANAGED_LAYOUT_BUNDLE_SHAPE)
    return [
        range(index, index + width)
        for index in range(len(block_types) - width + 1)
        if tuple(block_types[index : index + width]) == _MANAGED_LAYOUT_BUNDLE_SHAPE
    ]


def _run_audit_dashboard(args: argparse.Namespace) -> int:
    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")

        with NotionClient(credentials.notion_token) as notion:
            blocks = notion.list_block_children(page_id)
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    block_types = [str(block.get("type", "")) for block in blocks]
    marker_indexes = [index for index, block in enumerate(blocks) if _is_dashboard_marker(block)]
    managed_prefix = ("heading_1", "callout", "column_list", "divider", "callout")
    managed_bundle_candidates = sum(
        index >= len(managed_prefix)
        and tuple(block_types[index - len(managed_prefix) : index]) == managed_prefix
        for index in marker_indexes
    )
    layout_bundle_shape_candidates = len(_layout_bundle_ranges(blocks))
    child_database = block_types.count("child_database")
    column_list = block_types.count("column_list")
    marker_count = len(marker_indexes)
    other_blocks = len(blocks) - child_database - column_list - marker_count
    print(
        "Dashboard audit OK "
        f"(total={len(blocks)}, child_database={child_database}, "
        f"column_list={column_list}, marker_count={marker_count}, "
        f"managed_bundle_candidates={managed_bundle_candidates}, "
        f"layout_bundle_shape_candidates={layout_bundle_shape_candidates}, "
        f"other_blocks={other_blocks})"
    )
    return 0


def _run_audit_view_configurations(args: argparse.Namespace) -> int:
    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")

        with NotionClient(credentials.notion_token) as notion:
            rows = NotionInitializer(notion, page_id).view_configuration_counts(
                include_properties=args.details
            )
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    print("View configuration audit OK")
    for row in rows:
        count = row["properties_count"]
        count_label = "unknown" if count is None else str(count)
        visible_count = row.get("visible_properties_count")
        visible_label = "unknown" if visible_count is None else str(visible_count)
        view_id = str(row.get("view_id") or "")
        parent_database_id = str(row.get("parent_database_id") or "")
        line = f"- {row['name']}: configuration.properties={count_label}, visible={visible_label}"
        if args.details:
            known_count = row.get("known_properties_count")
            unknown_count = row.get("unknown_properties_count")
            known_label = "unknown" if known_count is None else str(known_count)
            unknown_label = "unknown" if unknown_count is None else str(unknown_count)
            line += f", known={known_label}, unknown={unknown_label}"
        line += f", view_id={view_id}, parent_database_id={parent_database_id}"
        print(line)
        if args.details:
            properties = row.get("properties")
            if isinstance(properties, list) and properties:
                for index, property_name in enumerate(properties, start=1):
                    print(f"  {index}. {property_name}")
    return 0


def _run_cleanup_dashboard_layout(args: argparse.Namespace) -> int:
    expected_blocks = args.expected_bundles * len(_MANAGED_LAYOUT_BUNDLE_SHAPE)
    expected_confirmation = (
        f"ARCHIVE_{args.expected_bundles}_BUNDLES_{expected_blocks}_LAYOUT_BLOCKS"
    )
    if (
        args.expected_bundles <= 0
        or args.expected_total <= 0
        or args.confirm != expected_confirmation
    ):
        print(
            "Dashboard layout cleanup refused: confirmation or expected counts did not match",
            file=sys.stderr,
        )
        return 7

    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")

        with NotionClient(credentials.notion_token) as notion:
            blocks = notion.list_block_children(page_id)
            ranges = _layout_bundle_ranges(blocks)
            child_database_count = sum(block.get("type") == "child_database" for block in blocks)
            column_list_count = sum(block.get("type") == "column_list" for block in blocks)
            if (
                len(blocks) != args.expected_total
                or len(ranges) != args.expected_bundles
                or child_database_count != 8
                or column_list_count != args.expected_bundles + 1
            ):
                print(
                    "Dashboard layout cleanup refused: root layout preflight "
                    "did not match "
                    f"(expected_total={args.expected_total}, actual_total={len(blocks)}, "
                    f"expected_bundles={args.expected_bundles}, "
                    f"actual_bundles={len(ranges)}, "
                    f"child_database={child_database_count}, "
                    f"column_list={column_list_count})",
                    file=sys.stderr,
                )
                return 7

            selected = [blocks[index] for bundle_range in ranges for index in bundle_range]
            block_ids = [block.get("id") for block in selected]
            if (
                len(block_ids) != expected_blocks
                or len(set(block_ids)) != expected_blocks
                or not all(isinstance(block_id, str) and block_id for block_id in block_ids)
            ):
                print(
                    "Dashboard layout cleanup refused: selected layout block IDs "
                    "were incomplete or duplicated",
                    file=sys.stderr,
                )
                return 7

            for block_id in block_ids:
                if not isinstance(block_id, str):
                    raise AssertionError("preflight validation did not narrow block ID")
                notion.delete_block(block_id)

            remaining = notion.list_block_children(page_id)
            remaining_child_databases = sum(
                block.get("type") == "child_database" for block in remaining
            )
            remaining_columns = sum(block.get("type") == "column_list" for block in remaining)
            remaining_candidates = len(_layout_bundle_ranges(remaining))
            expected_remaining = args.expected_total - expected_blocks
            if (
                len(remaining) != expected_remaining
                or remaining_child_databases != 8
                or remaining_columns != 1
                or remaining_candidates != 0
            ):
                print(
                    "Dashboard layout cleanup incomplete after archive "
                    f"(remaining_total={len(remaining)}, "
                    f"child_database={remaining_child_databases}, "
                    f"column_list={remaining_columns}, "
                    f"duplicate_bundles={remaining_candidates})",
                    file=sys.stderr,
                )
                return 7
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    print(
        "Dashboard layout cleanup OK "
        f"(bundles archived={args.expected_bundles}, "
        f"blocks archived={expected_blocks}, "
        f"remaining total={expected_remaining}, "
        "child_database=8, column_list=1)"
    )
    return 0


def _run_rebuild_dashboard(args: argparse.Namespace) -> int:
    expected_confirmation = f"ARCHIVE_{args.expected_count}_LINKED_DATABASE_BLOCKS"
    if args.expected_count <= 0 or args.confirm != expected_confirmation:
        print(
            "Dashboard rebuild refused: confirmation or expected count did not match",
            file=sys.stderr,
        )
        return 7

    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")

        with NotionClient(credentials.notion_token) as notion:
            child_databases = [
                block
                for block in notion.list_block_children(page_id)
                if block.get("type") == "child_database"
            ]
            if len(child_databases) != args.expected_count:
                print(
                    "Dashboard rebuild refused: actual root child_database count "
                    f"did not match (expected={args.expected_count}, "
                    f"actual={len(child_databases)})",
                    file=sys.stderr,
                )
                return 7

            block_ids = [block.get("id") for block in child_databases]
            if not all(isinstance(block_id, str) and block_id for block_id in block_ids):
                print(
                    "Dashboard rebuild refused: a root child_database block had no ID",
                    file=sys.stderr,
                )
                return 7

            for block_id in block_ids:
                if not isinstance(block_id, str):
                    raise AssertionError("preflight validation did not narrow block ID")
                notion.delete_block(block_id)
            remaining_count = sum(
                block.get("type") == "child_database"
                for block in notion.list_block_children(page_id)
            )
            if remaining_count:
                print(
                    "Dashboard rebuild refused: root child_database blocks remained "
                    f"after archive (remaining={remaining_count})",
                    file=sys.stderr,
                )
                return 7
            result = NotionInitializer(notion, page_id).initialize()
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    print(
        "Dashboard rebuild OK "
        f"(archived={len(block_ids)}, databases created={result.created_databases}, "
        f"views created={result.created_views}, views updated={result.updated_views}, "
        f"views deleted={getattr(result, 'deleted_views', 0)})"
    )
    return 0


_LEGACY_DASHBOARD_LAYOUT_TYPES = (
    "child_page",
    "heading_1",
    "callout",
    "column_list",
    "heading_2",
    "callout",
    "heading_2",
    "paragraph",
    "paragraph",
    "paragraph",
    "image",
    "child_database",
    "child_database",
    "child_database",
    "child_database",
    "child_database",
    "child_database",
    "child_database",
    "child_database",
)


def _is_data_page(block: Mapping[str, object]) -> bool:
    child_page = block.get("child_page")
    return (
        block.get("type") == "child_page"
        and isinstance(child_page, Mapping)
        and child_page.get("title") == DATA_PAGE_TITLE
    )


def _run_rebuild_dashboard_layout(args: argparse.Namespace) -> int:
    expected_confirmation = f"REBUILD_MANAGED_DASHBOARD_LAYOUT_{args.expected_total}_BLOCKS"
    if args.expected_total <= 0 or args.confirm != expected_confirmation:
        print(
            "Dashboard layout rebuild refused: confirmation or expected total did not match",
            file=sys.stderr,
        )
        return 7

    try:
        credentials = load_runtime_credentials()
        credentials.require("notion_token")
        page_id = args.page_id or credentials.notion_page_id
        if not page_id:
            raise MissingCredentialError(
                "Missing target page: set NOTION_PAGE_ID or pass --page-id"
            )
        if credentials.notion_token is None:
            raise AssertionError("credential requirement did not narrow notion_token")

        with NotionClient(credentials.notion_token) as notion:
            blocks = notion.list_block_children(page_id)
            block_types = tuple(str(block.get("type", "")) for block in blocks)
            data_pages = [block for block in blocks if _is_data_page(block)]
            if (
                len(blocks) != args.expected_total
                or args.expected_total != len(_LEGACY_DASHBOARD_LAYOUT_TYPES)
                or Counter(block_types) != Counter(_LEGACY_DASHBOARD_LAYOUT_TYPES)
                or len(data_pages) != 1
            ):
                print(
                    "Dashboard layout rebuild refused: root preflight did not match "
                    f"(expected_total={args.expected_total}, actual_total={len(blocks)}, "
                    f"data_pages={len(data_pages)}, "
                    f"child_database={block_types.count('child_database')}, "
                    f"column_list={block_types.count('column_list')}, "
                    f"type_counts={dict(sorted(Counter(block_types).items()))})",
                    file=sys.stderr,
                )
                return 7

            managed_blocks = [block for block in blocks if not _is_data_page(block)]
            block_ids = [block.get("id") for block in managed_blocks]
            if (
                len(block_ids) != args.expected_total - 1
                or len(set(block_ids)) != args.expected_total - 1
                or not all(isinstance(block_id, str) and block_id for block_id in block_ids)
            ):
                print(
                    "Dashboard layout rebuild refused: managed block IDs were incomplete "
                    "or duplicated",
                    file=sys.stderr,
                )
                return 7

            for block_id in block_ids:
                if not isinstance(block_id, str):
                    raise AssertionError("preflight validation did not narrow block ID")
                notion.delete_block(block_id)

            remaining = notion.list_block_children(page_id)
            if len(remaining) != 1 or not _is_data_page(remaining[0]):
                print(
                    "Dashboard layout rebuild stopped: target page did not reduce to the "
                    "single protected data page",
                    file=sys.stderr,
                )
                return 7
            result = NotionInitializer(notion, page_id).initialize(create_home=True)
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    print(
        "Dashboard layout rebuild OK "
        f"(managed blocks archived={len(block_ids)}, data pages preserved=1, "
        f"databases created={result.created_databases}, "
        f"views created={result.created_views}, views updated={result.updated_views}, "
        f"views deleted={getattr(result, 'deleted_views', 0)})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        protected_services = len(CredentialKind)
        protected_hosts = sum(len(allowed_hosts(kind)) for kind in CredentialKind)
        print(
            f"Xyz2Notion {__version__}: OK "
            f"({protected_services} credential types, {protected_hosts} allowed hosts)"
        )
        return 0
    if args.command == "config-check":
        try:
            config = load_config(args.config)
        except ConfigurationError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        providers = ", ".join(provider.value for provider in config.asr.provider_order) or "paused"
        print(f"Configuration OK (schema v{config.schema_version}, ASR: {providers})")
        return 0
    if args.command == "config-schema":
        print(config_schema_json())
        return 0
    if args.command == "notion-init":
        try:
            credentials = load_runtime_credentials()
            credentials.require("notion_token")
            page_id = args.page_id or credentials.notion_page_id
            if not page_id:
                raise MissingCredentialError(
                    "Missing target page: set NOTION_PAGE_ID or pass --page-id"
                )
            if credentials.notion_token is None:
                raise AssertionError("credential requirement did not narrow notion_token")
            with NotionClient(credentials.notion_token) as notion:
                result = NotionInitializer(notion, page_id).initialize(create_home=args.create_home)
        except (ConfigurationError, MissingCredentialError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except NotionAPIError as exc:
            print(f"Notion error: {exc}", file=sys.stderr)
            return 4
        print(
            "Notion initialization OK "
            f"(databases created: {result.created_databases}, "
            f"views created: {result.created_views}, views updated: {result.updated_views}, "
            f"views deleted: {getattr(result, 'deleted_views', 0)})"
        )
        return 0
    if args.command == "audit-dashboard":
        return _run_audit_dashboard(args)
    if args.command == "audit-view-configurations":
        return _run_audit_view_configurations(args)
    if args.command == "cleanup-dashboard-layout":
        return _run_cleanup_dashboard_layout(args)
    if args.command == "rebuild-dashboard":
        return _run_rebuild_dashboard(args)
    if args.command == "rebuild-dashboard-layout":
        return _run_rebuild_dashboard_layout(args)
    if args.command == "xiaoyuzhou-check":
        try:
            credentials = load_runtime_credentials()
            credentials.require("xiaoyuzhou_refresh_token")
            if credentials.xiaoyuzhou_refresh_token is None:
                raise AssertionError(
                    "credential requirement did not narrow xiaoyuzhou_refresh_token"
                )
            with XiaoyuzhouClient(
                credentials.xiaoyuzhou_refresh_token,
                credentials.xiaoyuzhou_device_id,
            ) as xiaoyuzhou:
                xiaoyuzhou.profile()
        except (ConfigurationError, MissingCredentialError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except XiaoyuzhouAPIError as exc:
            print(f"Xiaoyuzhou error: {exc}", file=sys.stderr)
            return 3
        print("Xiaoyuzhou authentication OK")
        return 0
    if args.command == "sync-metadata":
        try:
            credentials = load_runtime_credentials()
            credentials.require("xiaoyuzhou_refresh_token", "notion_token")
            page_id = args.page_id or credentials.notion_page_id
            if not page_id:
                raise MissingCredentialError(
                    "Missing target page: set NOTION_PAGE_ID or pass --page-id"
                )
            if credentials.xiaoyuzhou_refresh_token is None or credentials.notion_token is None:
                raise AssertionError("credential requirements did not narrow tokens")
            with (
                XiaoyuzhouClient(
                    credentials.xiaoyuzhou_refresh_token,
                    credentials.xiaoyuzhou_device_id,
                ) as xiaoyuzhou,
                NotionClient(credentials.notion_token) as notion,
            ):
                initialization = NotionInitializer(notion, page_id).initialize()
                snapshot = collect_metadata(xiaoyuzhou)
                played_count = sum(episode.played_seconds > 0 for episode in snapshot.episodes)
                playlist_count = sum(episode.in_playlist for episode in snapshot.episodes)
                favorite_count = sum(episode.favorited for episode in snapshot.episodes)
                report = MetadataSynchronizer(
                    notion,
                    initialization.resources,
                ).sync(snapshot)
                statistics_report = NotionIncrementalStatistics(
                    notion,
                    initialization.resources,
                    root_page_id=page_id,
                ).sync()
                heatmap_action = "baseline_preserved"
                if statistics_report.mode == "incremental":
                    heatmap_action = (
                        HeatmapPublisher(notion, page_id)
                        .publish(
                            local_today().year,
                            statistics_report.daily,
                        )
                        .action
                    )
        except (ConfigurationError, MissingCredentialError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except XiaoyuzhouAPIError as exc:
            print(f"Xiaoyuzhou error: {exc}", file=sys.stderr)
            return 3
        except NotionAPIError as exc:
            print(f"Notion error: {exc}", file=sys.stderr)
            return 4
        except ValueError as exc:
            print(f"Statistics error: {exc}", file=sys.stderr)
            return 6
        print(
            "Metadata synchronization OK "
            f"(created: {report.created}, updated: {report.updated}, "
            f"unchanged: {report.unchanged}; "
            f"statistics_mode: {statistics_report.mode}, "
            f"statistics_delta_seconds: {statistics_report.delta_seconds}, "
            f"statistics_total_seconds: {statistics_report.total_seconds}, "
            f"heatmap: {heatmap_action}; "
            f"episodes played: {played_count}, "
            f"playlist: {playlist_count}, favorites: {favorite_count})"
        )
        return 0
    if args.command == "process-asr":
        return _run_asr_queue(args)
    if args.command == "process-manual-retries":
        return _run_manual_retry_queue(args)
    if args.command == "repair-notion-covers":
        return _run_cover_repair(args)
    if args.command == "reconcile-published-ai":
        return _run_published_ai_reconciliation(args)
    if args.command == "audit-notion-backlog":
        return _run_notion_backlog_audit(args)
    if args.command == "archive-legacy-zero-play":
        return _run_archive_legacy_zero_play(args)
    if args.command == "reopen-timeline-failures":
        return _run_reopen_timeline_failures(args)
    if args.command == "reopen-summary-failures":
        return _run_reopen_all_summary_failures(args)
    if args.command == "migrate":
        return _run_migration(args)
    if args.command == "redo-episode":
        return _run_redo_episode(args)
    if args.command in {"rebuild-statistics", "rebuild-heatmap"}:
        return _run_rebuild(
            args,
            heatmap_only=args.command == "rebuild-heatmap",
        )

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
