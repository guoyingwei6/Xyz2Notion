"""Command-line interface for Xyz2Notion."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
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
from xyz2notion.notion.client import NotionAPIError, NotionClient
from xyz2notion.notion.initializer import NotionInitializer
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    build_provider_clients,
    episode_candidates,
)
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
    return parser


def _summary_policy(config: object) -> SummaryPolicy:
    summary = config.summary  # type: ignore[attr-defined]
    return SummaryPolicy(
        model=summary.model,
        prompt_version=summary.prompt_version,
        chunk_tokens=summary.chunk_tokens,
        chunk_minutes=summary.chunk_minutes,
        max_output_tokens=summary.max_output_tokens,
        input_cny_per_million_tokens=summary.input_cny_per_million_tokens,
        output_cny_per_million_tokens=summary.output_cny_per_million_tokens,
    )


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
        siliconflow_api_key = (
            credentials.siliconflow_api_key if AsrProvider.SILICONFLOW in selected else None
        )
        dashscope_api_key = credentials.dashscope_api_key if config.summary.enabled else None

        with ExitStack() as stack:
            notion = stack.enter_context(NotionClient(credentials.notion_token))
            initialization = NotionInitializer(notion, page_id).initialize()
            pages = notion.query_data_source(
                initialization.resources["episode"].data_source_id,
                {"page_size": 100},
            )
            candidates = episode_candidates(pages)[: config.limits.episodes_per_run]
            tingwu, siliconflow, dashscope = build_provider_clients(
                tingwu_cookie=tingwu_cookie,
                siliconflow_api_key=siliconflow_api_key,
                dashscope_api_key=dashscope_api_key,
                siliconflow_models=config.asr.siliconflow_models,
            )
            for client in (tingwu, siliconflow, dashscope):
                if client is not None:
                    stack.enter_context(client)
            state_store = stack.enter_context(NotionEpisodeStateStore(notion))
            processor = EpisodeAIProcessor(
                notion,
                state_store,
                tingwu=tingwu,
                siliconflow=siliconflow,
                dashscope=dashscope,
                summary_policy=_summary_policy(config),
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
        providers = ", ".join(provider.value for provider in config.asr.provider_order)
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
                result = NotionInitializer(notion, page_id).initialize()
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
                wrapped = collect_monthly_wrapped(xiaoyuzhou, snapshot)
                report = MetadataSynchronizer(
                    notion,
                    initialization.resources,
                ).sync(snapshot)
                statistics = calculate_statistics(snapshot, wrapped)
                statistics_report = StatisticsSynchronizer(
                    notion,
                    initialization.resources,
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
            f"heatmap: {heatmap.action})"
        )
        return 0
    if args.command in {"process-ai", "retry-failed"}:
        return _run_ai(args, retry_failed=args.command == "retry-failed")

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
