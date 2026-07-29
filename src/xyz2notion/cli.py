"""Command-line interface for Xyz2Notion."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import date

from xyz2notion import __version__
from xyz2notion.config import (
    AsrProvider,
    ConfigurationError,
    MissingCredentialError,
    config_schema_json,
    load_config,
    load_runtime_credentials,
)
from xyz2notion.enrichment.pipeline import SummaryPolicy
from xyz2notion.migration.legacy import LegacyTemplateMigrator
from xyz2notion.migration.schema import (
    CURRENT_WORKSPACE_SCHEMA_VERSION,
    detect_workspace_schema_version,
    migration_plan,
)
from xyz2notion.notion.client import JsonObject, NotionAPIError, NotionClient
from xyz2notion.notion.initializer import DATA_PAGE_TITLE, HOME_MARKER_URL, NotionInitializer
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    build_provider_clients,
    episode_candidates,
)
from xyz2notion.orchestration.recovery import reset_episode_ai
from xyz2notion.orchestration.state_store import NotionEpisodeStateStore
from xyz2notion.security import CredentialKind, allowed_hosts
from xyz2notion.statistics.calculator import calculate_statistics
from xyz2notion.statistics.notion_sync import HeatmapPublisher, StatisticsSynchronizer
from xyz2notion.sync.metadata import MetadataSynchronizer
from xyz2notion.sync.pipeline import collect_metadata, collect_monthly_wrapped
from xyz2notion.xiaoyuzhou.client import XiaoyuzhouAPIError, XiaoyuzhouClient


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
    for name, help_text in (
        ("process-ai", "advance ASR, summary, and Notion publishing"),
        ("retry-failed", "retry only resumable failed Episode AI jobs"),
    ):
        ai_command = subparsers.add_parser(name, help=help_text)
        ai_command.add_argument("--config", default="config.yaml", help="path to config.yaml")
        ai_command.add_argument(
            "--page-id",
            help="target root page ID; defaults to NOTION_PAGE_ID",
        )
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


def _summary_policy(config: object) -> SummaryPolicy:
    summary = config.summary  # type: ignore[attr-defined]
    return SummaryPolicy(
        prompt_version=summary.prompt_version,
        chunk_tokens=summary.chunk_tokens,
        chunk_minutes=summary.chunk_minutes,
        max_output_tokens=summary.max_output_tokens,
    )


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


def _eligible_ai_pages(
    pages: Sequence[JsonObject],
    *,
    retry_failed: bool,
) -> list[JsonObject]:
    return [page for page in pages if (_episode_asr_status(page) == "可重试失败") is retry_failed]


def _run_ai(args: argparse.Namespace, *, retry_failed: bool) -> int:
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

        selected = set(config.asr.provider_order)
        tingwu_cookie = credentials.tingwu_cookie if AsrProvider.TINGWU_COOKIE in selected else None
        siliconflow_asr_api_key = (
            credentials.siliconflow_api_key if AsrProvider.SILICONFLOW in selected else None
        )
        local_whisper_model = (
            config.asr.local_whisper_model if AsrProvider.LOCAL_WHISPER in selected else None
        )
        siliconflow_summary_api_key = (
            credentials.siliconflow_api_key if config.summary.enabled else None
        )

        with ExitStack() as stack:
            notion = stack.enter_context(NotionClient(credentials.notion_token))
            initialization = NotionInitializer(notion, page_id).initialize()
            pages = notion.query_data_source(
                initialization.resources["episode"].data_source_id,
                {"page_size": 100},
            )
            eligible_pages = _eligible_ai_pages(pages, retry_failed=retry_failed)
            candidates = episode_candidates(eligible_pages)[: config.limits.episodes_per_run]
            tingwu, siliconflow, local_whisper, summary_client = build_provider_clients(
                tingwu_cookie=tingwu_cookie,
                siliconflow_asr_api_key=siliconflow_asr_api_key,
                siliconflow_summary_api_key=siliconflow_summary_api_key,
                siliconflow_asr_models=config.asr.siliconflow_models,
                siliconflow_summary_models=config.summary.siliconflow_models,
                local_whisper_model=local_whisper_model,
            )
            for client in (tingwu, siliconflow, local_whisper, summary_client):
                if client is not None:
                    stack.enter_context(client)
            state_store = stack.enter_context(NotionEpisodeStateStore(notion))
            processor = EpisodeAIProcessor(
                notion,
                state_store,
                tingwu=tingwu,
                siliconflow=siliconflow,
                local_whisper=local_whisper,
                summary_client=summary_client,
                summary_policy=_summary_policy(config),
                summary_enabled=config.summary.enabled,
            )
            page_by_id = {str(page.get("id")): page for page in pages}
            outcomes = [
                processor.process(
                    candidate,
                    page_by_id[candidate.page_id],
                    retry_failed=retry_failed,
                    only_failed=retry_failed,
                )
                for candidate in candidates
            ]
    except (ConfigurationError, MissingCredentialError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except NotionAPIError as exc:
        print(f"Notion error: {exc}", file=sys.stderr)
        return 4

    actions = Counter(outcome.action for outcome in outcomes)
    states = Counter(outcome.state.value for outcome in outcomes)
    action_summary = ", ".join(f"{name}={actions[name]}" for name in sorted(actions)) or "none=0"
    state_summary = ", ".join(f"{name}={states[name]}" for name in sorted(states)) or "none=0"
    print(
        f"Episode AI processing OK (selected={len(candidates)}; "
        f"actions: {action_summary}; states: {state_summary})"
    )
    return 0


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
            wrapped = collect_monthly_wrapped(xiaoyuzhou, snapshot)
            statistics = calculate_statistics(snapshot, wrapped)
            if heatmap_only:
                heatmap = HeatmapPublisher(notion, page_id).publish(
                    date.today().year,
                    statistics.daily,
                )
                output = f"Heatmap rebuild OK (action={heatmap.action})"
            else:
                report = StatisticsSynchronizer(
                    notion,
                    initialization.resources,
                    page_id,
                ).sync(statistics)
                output = (
                    "Statistics rebuild OK "
                    f"(created={report.created}, updated={report.updated}, "
                    f"unchanged={report.unchanged})"
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
    print(output)
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
        f"views created={result.created_views}, views updated={result.updated_views})"
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
        f"views created={result.created_views}, views updated={result.updated_views})"
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
            f"views created: {result.created_views}, views updated: {result.updated_views})"
        )
        return 0
    if args.command == "audit-dashboard":
        return _run_audit_dashboard(args)
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
                wrapped = collect_monthly_wrapped(xiaoyuzhou, snapshot)
                report = MetadataSynchronizer(
                    notion,
                    initialization.resources,
                ).sync(snapshot)
                statistics = calculate_statistics(snapshot, wrapped)
                statistics_report = StatisticsSynchronizer(
                    notion,
                    initialization.resources,
                    page_id,
                ).sync(statistics)
                heatmap = HeatmapPublisher(notion, page_id).publish(
                    date.today().year,
                    statistics.daily,
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
        print(
            "Metadata synchronization OK "
            f"(created: {report.created}, updated: {report.updated}, "
            f"unchanged: {report.unchanged}; "
            f"statistics created: {statistics_report.created}, "
            f"statistics updated: {statistics_report.updated}; "
            f"episodes played: {played_count}, "
            f"playlist: {playlist_count}, favorites: {favorite_count}; "
            f"heatmap: {heatmap.action})"
        )
        return 0
    if args.command in {"process-ai", "retry-failed"}:
        return _run_ai(args, retry_failed=args.command == "retry-failed")
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
