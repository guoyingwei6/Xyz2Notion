from types import SimpleNamespace
from typing import ClassVar

import pytest

import xyz2notion.cli as cli_module
from xyz2notion import __version__
from xyz2notion.cli import main
from xyz2notion.models import (
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
    TranscriptSegment,
)
from xyz2notion.orchestration.state_store import EpisodeAIState
from xyz2notion.state import PipelineRecord, PipelineState


def test_doctor_reports_installation(capsys: object) -> None:
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert f"Xyz2Notion {__version__}: OK" in output
    assert "4 credential types" in output


def test_help_is_default(capsys: object) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "doctor" in output


def test_config_check_accepts_example(capsys: object) -> None:
    assert main(["config-check", "--config", "config.example.yaml"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Configuration OK" in output
    assert "ASR: dashscope, siliconflow, local_whisper" in output


def test_config_check_reports_missing_file(capsys: object) -> None:
    assert main(["config-check", "--config", "missing.yaml"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Configuration error" in error


def test_config_schema_command(capsys: object) -> None:
    assert main(["config-schema"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert '"schema_version"' in output


def test_notion_init_reports_missing_token(capsys: object, monkeypatch: object) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert main(["notion-init"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Missing required credential" in error


def test_xiaoyuzhou_check_reports_missing_token(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("XIAOYUZHOU_REFRESH_TOKEN", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("REFRESH_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["xiaoyuzhou-check"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Missing required credential" in error


def test_sync_metadata_reports_missing_tokens(
    capsys: object,
    monkeypatch: object,
) -> None:
    for name in (
        "XIAOYUZHOU_REFRESH_TOKEN",
        "REFRESH_TOKEN",
        "NOTION_TOKEN",
        "NOTION_PAGE_ID",
    ):
        monkeypatch.delenv(name, raising=False)  # type: ignore[attr-defined]
    assert main(["sync-metadata"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Missing required credential" in error


def test_migrate_reports_missing_notion_token(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["migrate", "--dry-run"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Missing required credential" in error


class FakeContextClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "FakeContextClient":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def profile(self) -> dict[str, str]:
        return {"uid": "never-printed"}

    def list_block_children(self, _block_id: str) -> list[object]:
        return []

    def delete_block(self, _block_id: str) -> dict[str, bool]:
        return {"archived": True}


def test_xiaoyuzhou_check_succeeds_without_printing_identity(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("XIAOYUZHOU_REFRESH_TOKEN", "fixture-refresh")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "XiaoyuzhouClient", FakeContextClient)  # type: ignore[attr-defined]
    assert main(["xiaoyuzhou-check"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert output.strip() == "Xiaoyuzhou authentication OK"
    assert "never-printed" not in output


def test_sync_metadata_success_reports_only_counts(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("XIAOYUZHOU_REFRESH_TOKEN", "fixture-refresh")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "XiaoyuzhouClient", FakeContextClient)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]
    fixture_snapshot = SimpleNamespace(
        episodes=(
            SimpleNamespace(played_seconds=120, in_playlist=True, favorited=False),
            SimpleNamespace(played_seconds=0, in_playlist=True, favorited=True),
        )
    )

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self) -> object:
            return SimpleNamespace(resources={})

    class FakeSynchronizer:
        def __init__(self, _api: object, resources: object) -> None:
            assert resources == {}

        def sync(self, snapshot: object) -> object:
            assert snapshot is fixture_snapshot
            return SimpleNamespace(created=2, updated=1, unchanged=3)

    class FakeStatistics:
        def __init__(
            self,
            _api: object,
            resources: object,
            *,
            root_page_id: str,
        ) -> None:
            assert resources == {}
            assert root_page_id == "fixture-page"

        def sync(self) -> object:
            return SimpleNamespace(
                mode="baseline",
                delta_seconds=0,
                total_seconds=522_000,
                daily=(),
            )

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "MetadataSynchronizer", FakeSynchronizer)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionIncrementalStatistics",
        FakeStatistics,
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "collect_metadata",
        lambda _api, **_kwargs: fixture_snapshot,
    )
    assert main(["sync-metadata"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "created: 2, updated: 1, unchanged: 3" in output
    assert "statistics_mode: baseline" in output
    assert "statistics_delta_seconds: 0" in output
    assert "statistics_total_seconds: 522000" in output
    assert "heatmap: baseline_preserved" in output
    assert "episodes played: 1, playlist: 2, favorites: 1" in output


def test_notion_init_success_reports_counts(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self, *, create_home: bool = False) -> object:
            assert create_home is False
            return SimpleNamespace(
                created_databases=9,
                created_views=12,
                updated_views=0,
            )

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    assert main(["notion-init"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "databases created: 9" in output
    assert "views created: 12" in output


def test_notion_bootstrap_requests_home_creation(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self, *, create_home: bool = False) -> object:
            assert create_home is True
            return SimpleNamespace(
                created_databases=9,
                created_views=19,
                updated_views=0,
            )

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    assert main(["notion-init", "--create-home"]) == 0
    assert "views created: 19" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_audit_dashboard_reports_only_aggregate_counts_without_writes(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    marker = {
        "id": "private-marker-id",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": "private marker text",
                        "link": {"url": cli_module.HOME_MARKER_URL},
                    },
                }
            ]
        },
    }

    class FakeNotion(FakeContextClient):
        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            return [
                {"id": "private-heading", "type": "heading_1"},
                {"id": "private-callout-1", "type": "callout"},
                {"id": "private-columns", "type": "column_list"},
                {"id": "private-divider", "type": "divider"},
                {"id": "private-callout-2", "type": "callout"},
                marker,
                {"id": "private-database", "type": "child_database"},
                {
                    "id": "private-plain-url",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": cli_module.HOME_MARKER_URL,
                                    "link": None,
                                },
                            }
                        ]
                    },
                },
                marker,
            ]

        def delete_block(self, _block_id: str) -> dict[str, bool]:
            raise AssertionError("audit must not delete blocks")

    class ForbiddenInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            raise AssertionError("audit must not initialize Notion")

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionInitializer",
        ForbiddenInitializer,
    )
    assert main(["audit-dashboard"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert output.strip() == (
        "Dashboard audit OK (total=9, child_database=1, column_list=1, "
        "marker_count=2, managed_bundle_candidates=1, "
        "layout_bundle_shape_candidates=1, other_blocks=5)"
    )
    assert "private-" not in output
    assert cli_module.HOME_MARKER_URL not in output


def test_audit_dashboard_reports_missing_token(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["audit-dashboard", "--page-id", "fixture-page"]) == 2
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_audit_view_configurations_reports_counts(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def view_configuration_counts(
            self, *, include_properties: bool = False
        ) -> list[dict[str, object]]:
            assert include_properties is False
            return [
                {
                    "data_source_id": "private-source-id",
                    "view_id": "private-view-id",
                    "parent_database_id": "private-database-id",
                    "name": "转写文本",
                    "properties_count": 100,
                    "visible_properties_count": 5,
                },
                {
                    "data_source_id": "private-source-id",
                    "view_id": "private-view-id-2",
                    "name": "AI总结与思维导图",
                    "properties_count": 5,
                    "visible_properties_count": 5,
                },
            ]

        def initialize(self, **_kwargs: object) -> object:
            raise AssertionError("audit must not initialize Notion")

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert main(["audit-view-configurations"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "View configuration audit OK" in output
    assert "- 转写文本: configuration.properties=100, visible=5" in output
    assert "- AI总结与思维导图: configuration.properties=5, visible=5" in output
    assert "view_id=private-view-id" in output
    assert "view_id=private-view-id-2" in output
    assert "parent_database_id=private-database-id" in output


def test_audit_view_configurations_details_reports_names_not_ids(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def view_configuration_counts(
            self, *, include_properties: bool = False
        ) -> list[dict[str, object]]:
            assert include_properties is True
            return [
                {
                    "data_source_id": "private-source-id",
                    "view_id": "private-view-id",
                    "parent_database_id": "private-database-id",
                    "name": "转写文本",
                    "properties_count": 2,
                    "visible_properties_count": 2,
                    "known_properties_count": 2,
                    "unknown_properties_count": 0,
                    "properties": ["Name", "人工请求重试"],
                }
            ]

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert main(["audit-view-configurations", "--details"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "- 转写文本: configuration.properties=2, visible=2, known=2, unknown=0" in output
    assert "view_id=private-view-id" in output
    assert "parent_database_id=private-database-id" in output
    assert "  1. Name" in output
    assert "  2. 人工请求重试" in output


def test_audit_view_configurations_reports_missing_token(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]

    assert main(["audit-view-configurations", "--page-id", "fixture-page"]) == 2
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_audit_view_configurations_reports_notion_error(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]

    class FailingInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def view_configuration_counts(
            self, *, include_properties: bool = False
        ) -> list[dict[str, object]]:
            assert include_properties is True
            raise cli_module.NotionAPIError("private upstream response")

    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FailingInitializer)  # type: ignore[attr-defined]

    assert main(["audit-view-configurations", "--page-id", "fixture-page", "--details"]) == 4
    assert "Notion error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_audit_dashboard_reports_notion_error(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]

    class FailingNotion(FakeContextClient):
        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "explicit-page"
            raise cli_module.NotionAPIError("private upstream response")

    monkeypatch.setattr(cli_module, "NotionClient", FailingNotion)  # type: ignore[attr-defined]
    assert main(["audit-dashboard", "--page-id", "explicit-page"]) == 4
    assert "Notion error" in capsys.readouterr().err  # type: ignore[attr-defined]


def _layout_bundle(prefix: str, *, complete: bool = True) -> list[dict[str, object]]:
    types = ["heading_1", "callout", "column_list", "divider", "callout"]
    if complete:
        types.append("paragraph")
    return [
        {"id": f"{prefix}-{index}", "type": block_type} for index, block_type in enumerate(types)
    ]


def test_cleanup_dashboard_layout_refuses_wrong_confirmation_before_api_access(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("API accessed")),
    )
    assert (
        main(
            [
                "cleanup-dashboard-layout",
                "--confirm",
                "wrong",
                "--expected-bundles",
                "5",
                "--expected-total",
                "49",
            ]
        )
        == 7
    )
    assert "cleanup refused" in capsys.readouterr().err.lower()  # type: ignore[attr-defined]


def test_cleanup_dashboard_layout_reports_missing_token(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "cleanup-dashboard-layout",
                "--page-id",
                "fixture-page",
                "--confirm",
                "ARCHIVE_5_BUNDLES_30_LAYOUT_BLOCKS",
                "--expected-bundles",
                "5",
                "--expected-total",
                "49",
            ]
        )
        == 2
    )
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_cleanup_dashboard_layout_reports_notion_error(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]

    class FailingNotion(FakeContextClient):
        def list_block_children(self, _block_id: str) -> list[object]:
            raise cli_module.NotionAPIError("private upstream response")

    monkeypatch.setattr(cli_module, "NotionClient", FailingNotion)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "cleanup-dashboard-layout",
                "--page-id",
                "fixture-page",
                "--confirm",
                "ARCHIVE_5_BUNDLES_30_LAYOUT_BLOCKS",
                "--expected-bundles",
                "5",
                "--expected-total",
                "49",
            ]
        )
        == 4
    )
    assert "Notion error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_cleanup_dashboard_layout_refuses_preflight_mismatch_without_archiving(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            return [{"id": "user-note", "type": "paragraph"}]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "cleanup-dashboard-layout",
                "--confirm",
                "ARCHIVE_5_BUNDLES_30_LAYOUT_BLOCKS",
                "--expected-bundles",
                "5",
                "--expected-total",
                "49",
            ]
        )
        == 7
    )
    assert FakeNotion.deleted == []
    assert "actual_total=1" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_cleanup_dashboard_layout_refuses_incomplete_selected_ids(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    duplicates = [block for bundle in range(5) for block in _layout_bundle(f"duplicate-{bundle}")]
    duplicates[0]["id"] = None
    original = [
        *duplicates,
        *_layout_bundle("preserved", complete=False),
        *[{"id": f"database-{index}", "type": "child_database"} for index in range(8)],
        {"id": "data-page", "type": "child_page"},
        {"id": "heatmap", "type": "image"},
        {"id": "user-note", "type": "paragraph"},
        {"id": "user-heading", "type": "heading_2"},
        {"id": "user-callout", "type": "callout"},
        {"id": "user-divider", "type": "divider"},
    ]

    class FakeNotion(FakeContextClient):
        def list_block_children(self, _block_id: str) -> list[object]:
            return original

        def delete_block(self, _block_id: str) -> dict[str, bool]:
            raise AssertionError("cleanup must not archive invalid selections")

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "cleanup-dashboard-layout",
                "--confirm",
                "ARCHIVE_5_BUNDLES_30_LAYOUT_BLOCKS",
                "--expected-bundles",
                "5",
                "--expected-total",
                "49",
            ]
        )
        == 7
    )
    assert "incomplete or duplicated" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_cleanup_dashboard_layout_archives_only_five_exact_duplicate_bundles(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    duplicate_blocks = [
        block for bundle in range(5) for block in _layout_bundle(f"duplicate-{bundle}")
    ]
    preserved_layout = _layout_bundle("preserved", complete=False)
    preserved_databases = [
        {"id": f"database-{index}", "type": "child_database"} for index in range(8)
    ]
    preserved_other = [
        {"id": "data-page", "type": "child_page"},
        {"id": "heatmap", "type": "image"},
        {"id": "user-note", "type": "paragraph"},
        {"id": "user-heading", "type": "heading_2"},
        {"id": "user-callout", "type": "callout"},
        {"id": "user-divider", "type": "divider"},
    ]
    original = [
        *duplicate_blocks,
        *preserved_layout,
        *preserved_databases,
        *preserved_other,
    ]
    assert len(original) == 49

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            return [block for block in original if block["id"] not in self.deleted]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "cleanup-dashboard-layout",
                "--confirm",
                "ARCHIVE_5_BUNDLES_30_LAYOUT_BLOCKS",
                "--expected-bundles",
                "5",
                "--expected-total",
                "49",
            ]
        )
        == 0
    )
    assert FakeNotion.deleted == [str(block["id"]) for block in duplicate_blocks]
    assert not set(FakeNotion.deleted) & {
        str(block["id"]) for block in [*preserved_layout, *preserved_databases, *preserved_other]
    }
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "bundles archived=5" in output
    assert "blocks archived=30" in output
    assert "remaining total=19" in output
    assert "duplicate-" not in output


def test_rebuild_dashboard_refuses_wrong_confirmation_before_api_access(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("API accessed")),
    )
    assert (
        main(
            [
                "rebuild-dashboard",
                "--confirm",
                "wrong",
                "--expected-count",
                "180",
            ]
        )
        == 7
    )
    assert "rebuild refused" in capsys.readouterr().err.lower()  # type: ignore[attr-defined]


def test_rebuild_dashboard_refuses_nonpositive_expected_count_before_api_access(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("API accessed")),
    )
    assert (
        main(
            [
                "rebuild-dashboard",
                "--confirm",
                "ARCHIVE_0_LINKED_DATABASE_BLOCKS",
                "--expected-count",
                "0",
            ]
        )
        == 7
    )
    assert "rebuild refused" in capsys.readouterr().err.lower()  # type: ignore[attr-defined]


def test_rebuild_dashboard_refuses_unexpected_actual_count_without_archiving(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            return [
                {"id": "linked-1", "type": "child_database"},
                {"id": "paragraph-1", "type": "paragraph"},
            ]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "rebuild-dashboard",
                "--confirm",
                "ARCHIVE_180_LINKED_DATABASE_BLOCKS",
                "--expected-count",
                "180",
            ]
        )
        == 7
    )
    assert FakeNotion.deleted == []
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "expected=180, actual=1" in error


def test_rebuild_dashboard_archives_only_exact_root_child_databases(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            if self.deleted:
                return [
                    {"id": "paragraph-1", "type": "paragraph"},
                    {"id": "column-list-1", "type": "column_list"},
                ]
            return [
                *[{"id": f"linked-{index}", "type": "child_database"} for index in range(180)],
                {"id": "paragraph-1", "type": "paragraph"},
                {"id": "column-list-1", "type": "column_list"},
            ]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self) -> object:
            assert FakeNotion.deleted == [f"linked-{index}" for index in range(180)]
            return SimpleNamespace(
                created_databases=0,
                created_views=14,
                updated_views=0,
            )

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "rebuild-dashboard",
                "--confirm",
                "ARCHIVE_180_LINKED_DATABASE_BLOCKS",
                "--expected-count",
                "180",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "archived=180" in output
    assert "views created=14" in output
    assert "linked-" not in output


def test_rebuild_dashboard_refuses_initialize_when_child_database_remains(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            if self.deleted:
                return [{"id": "linked-remains", "type": "child_database"}]
            return [
                {"id": "linked-1", "type": "child_database"},
                {"id": "linked-2", "type": "child_database"},
            ]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    class ForbiddenInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            raise AssertionError("initializer must not run while a linked block remains")

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionInitializer",
        ForbiddenInitializer,
    )
    assert (
        main(
            [
                "rebuild-dashboard",
                "--confirm",
                "ARCHIVE_2_LINKED_DATABASE_BLOCKS",
                "--expected-count",
                "2",
            ]
        )
        == 7
    )
    assert FakeNotion.deleted == ["linked-1", "linked-2"]
    assert "remaining=1" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_rebuild_dashboard_layout_refuses_wrong_confirmation_before_api_access(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("API accessed")),
    )
    assert (
        main(
            [
                "rebuild-dashboard-layout",
                "--confirm",
                "wrong",
                "--expected-total",
                "19",
            ]
        )
        == 7
    )
    assert "rebuild refused" in capsys.readouterr().err.lower()  # type: ignore[attr-defined]


def test_rebuild_dashboard_layout_refuses_nonmatching_root_without_archiving(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            return [
                {
                    "id": "data-page",
                    "type": "child_page",
                    "child_page": {"title": "Xyz2Notion 数据层"},
                },
                {"id": "user-note", "type": "paragraph"},
            ]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "rebuild-dashboard-layout",
                "--confirm",
                "REBUILD_MANAGED_DASHBOARD_LAYOUT_19_BLOCKS",
                "--expected-total",
                "19",
            ]
        )
        == 7
    )
    assert FakeNotion.deleted == []
    assert "actual_total=2" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_rebuild_dashboard_layout_preserves_data_page_and_bootstraps_home(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    layout_types = [
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
        *(["child_database"] * 8),
    ]
    # Notion may reorder managed root blocks while preserving the exact set.
    layout_types = [layout_types[0], *reversed(layout_types[1:])]

    class FakeNotion(FakeContextClient):
        deleted: ClassVar[list[str]] = []

        def list_block_children(self, block_id: str) -> list[object]:
            assert block_id == "fixture-page"
            data = {
                "id": "data-page",
                "type": "child_page",
                "child_page": {"title": "Xyz2Notion 数据层"},
            }
            if self.deleted:
                return [data]
            return [
                data
                if block_type == "child_page"
                else {"id": f"managed-{index}", "type": block_type}
                for index, block_type in enumerate(layout_types)
            ]

        def delete_block(self, block_id: str) -> dict[str, bool]:
            self.deleted.append(block_id)
            return {"archived": True}

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self, *, create_home: bool = False) -> object:
            assert create_home is True
            assert len(FakeNotion.deleted) == 18
            assert "data-page" not in FakeNotion.deleted
            return SimpleNamespace(
                created_databases=0,
                created_views=13,
                updated_views=0,
            )

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "rebuild-dashboard-layout",
                "--confirm",
                "REBUILD_MANAGED_DASHBOARD_LAYOUT_19_BLOCKS",
                "--expected-total",
                "19",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "managed blocks archived=18" in output
    assert "data pages preserved=1" in output
    assert "managed-" not in output


def test_episode_asr_status_distinguishes_retryable_rows() -> None:
    assert (
        cli_module._episode_asr_status(  # type: ignore[attr-defined]
            {
                "properties": {
                    "ASR Status": {"select": {"name": "可重试失败"}},
                }
            }
        )
        == "可重试失败"
    )
    assert cli_module._episode_asr_status({"properties": {}}) == ""  # type: ignore[attr-defined]
    assert (  # type: ignore[attr-defined]
        cli_module._episode_asr_status({"properties": {"ASR Status": {"select": "bad"}}}) == ""
    )


def test_ai_pages_are_filtered_before_the_per_run_limit() -> None:
    normal_1 = {"id": "normal-1", "properties": {}}
    normal_2 = {"id": "normal-2", "properties": {}}
    retryable = {
        "id": "retryable",
        "properties": {"ASR Status": {"select": {"name": "可重试失败"}}},
    }
    pages = [normal_1, normal_2, retryable]
    assert cli_module._eligible_ai_pages(pages, retry_failed=False)[:1] == [normal_1]  # type: ignore[attr-defined]
    assert cli_module._eligible_ai_pages(pages, retry_failed=True)[:1] == [retryable]  # type: ignore[attr-defined]


def test_ai_pages_prioritize_persisted_checkpoints() -> None:
    pages = [
        {"properties": {"ASR Status": {"select": {"name": "待处理"}}}},
        {"properties": {"ASR Status": {"select": {"name": "排队中"}}}},
        {"properties": {"ASR Status": {"select": {"name": "已转写"}}}},
        {"properties": {"ASR Status": {"select": {"name": "已增强"}}}},
    ]
    ordered = sorted(pages, key=cli_module._ai_page_priority)  # type: ignore[attr-defined]
    assert [cli_module._episode_asr_status(page) for page in ordered] == [  # type: ignore[attr-defined]
        "已增强",
        "已转写",
        "排队中",
        "待处理",
    ]
    assert cli_module._ai_page_priority({"properties": {}}) == (5, 5, "")  # type: ignore[attr-defined]


def test_notion_cover_repair_requires_limit_bound_confirmation(
    capsys: object,
) -> None:
    assert (
        main(
            [
                "repair-notion-covers",
                "--limit",
                "10",
                "--confirm",
                "wrong",
            ]
        )
        == 2
    )
    assert "REPAIR_10_NOTION_COVERS" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_published_ai_reconciliation_requires_bound_confirmation(
    capsys: object,
) -> None:
    assert (
        main(
            [
                "reconcile-published-ai",
                "--limit",
                "2",
                "--confirm",
                "wrong",
            ]
        )
        == 2
    )
    assert "RECONCILE_2_PUBLISHED_AI" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_notion_only_repair_reports_missing_credentials(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "repair-notion-covers",
                "--limit",
                "1",
                "--confirm",
                "REPAIR_1_NOTION_COVERS",
            ]
        )
        == 2
    )
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_notion_only_repair_reports_missing_target_page(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "repair-notion-covers",
                "--limit",
                "1",
                "--confirm",
                "REPAIR_1_NOTION_COVERS",
            ]
        )
        == 2
    )
    assert "Missing target page" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_notion_cover_repair_runs_without_xiaoyuzhou_credentials(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        pass

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self) -> object:
            return SimpleNamespace(
                resources={
                    "podcast": SimpleNamespace(data_source_id="podcasts"),
                    "episode": SimpleNamespace(data_source_id="episodes"),
                }
            )

    class FakeLocalizer:
        def __init__(
            self,
            _api: object,
            sources: object,
            *,
            sort_property: str,
        ) -> None:
            assert sources == ("podcasts",)
            assert sort_property == "Total Listening Seconds"

        def __enter__(self) -> "FakeLocalizer":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def repair(self, *, limit: int) -> object:
            assert limit == 1
            return SimpleNamespace(repaired=1, skipped=2, failed=0)

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionCoverLocalizer", FakeLocalizer)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "repair-notion-covers",
                "--limit",
                "1",
                "--confirm",
                "REPAIR_1_NOTION_COVERS",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "repaired=1" in output
    assert "failed=0" in output


def test_published_ai_reconciliation_runs_notion_only(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        def query_data_source_page(
            self,
            data_source_id: str,
            payload: object,
        ) -> list[object]:
            assert data_source_id == "episodes"
            assert payload == {
                "page_size": 2,
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
                            ],
                        },
                    ],
                },
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
            }
            return [{"id": "published"}]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def initialize(self) -> object:
            return SimpleNamespace(
                resources={
                    "episode": SimpleNamespace(data_source_id="episodes"),
                    "mindmap": SimpleNamespace(data_source_id="mindmaps"),
                }
            )

    class FakeStore:
        def __init__(self, _api: object) -> None:
            pass

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class FakeReconciler:
        def __init__(self, _api: object, _store: object, source: str) -> None:
            assert source == "mindmaps"

        def reconcile(self, pages: object, *, limit: int) -> object:
            assert pages == [{"id": "published"}]
            assert limit == 2
            return SimpleNamespace(
                selected=1,
                transcripts=1,
                summaries=1,
                page_ready=1,
                mindmaps_created=1,
                mindmaps_updated=0,
                mindmaps_unchanged=0,
                incomplete=0,
            )

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", FakeStore)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "PublishedAIReconciler", FakeReconciler)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "reconcile-published-ai",
                "--limit",
                "2",
                "--confirm",
                "RECONCILE_2_PUBLISHED_AI",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "transcripts=1" in output
    assert "mindmaps_created=1" in output


def test_archive_legacy_zero_play_trashes_only_unprotected_pages(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    pages = [
        {
            "id": "legacy-page",
            "properties": {
                "Played Seconds": {"number": 0},
                "In Playlist": {"checkbox": False},
                "Favorited": {"checkbox": False},
                "Liked": {"checkbox": False},
                "ASR Status": {"select": {"name": "待处理"}},
                "Name": {"title": [{"plain_text": "private legacy title"}]},
            },
        },
        {
            "id": "protected-page",
            "properties": {
                "Played Seconds": {"number": 0},
                "In Playlist": {"checkbox": True},
                "Favorited": {"checkbox": False},
                "Liked": {"checkbox": False},
                "ASR Status": {"select": {"name": "待处理"}},
            },
        },
        {
            "id": "played-page",
            "properties": {
                "Played Seconds": {"number": 1},
                "In Playlist": {"checkbox": False},
                "Favorited": {"checkbox": False},
                "Liked": {"checkbox": False},
                "ASR Status": {"select": {"name": "待处理"}},
            },
        },
        {"id": "already-trashed", "in_trash": True},
    ]
    updates: list[tuple[str, object]] = []
    trashed: set[str] = set()

    class FakeNotion(FakeContextClient):
        def query_data_source(self, source: str) -> list[dict[str, object]]:
            assert source == "episodes"
            return [page for page in pages if str(page["id"]) not in trashed]

        def update_page(self, page_id: str, payload: object) -> dict[str, object]:
            updates.append((page_id, payload))
            trashed.add(page_id)
            return {"id": page_id, "in_trash": True}

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module.time, "sleep", no_sleep)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1",
                "--confirm",
                "ARCHIVE_1_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "selected=1" in output
    assert "archived=1" in output
    assert "protected_zero_play=1" in output
    assert "legacy-page" not in output
    assert "private legacy title" not in output
    assert updates == [("legacy-page", {"in_trash": True})]


def test_archive_legacy_zero_play_refuses_count_drift_without_changes(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    updates: list[tuple[str, object]] = []

    class FakeNotion(FakeContextClient):
        def query_data_source(self, _source: str) -> list[dict[str, object]]:
            return [
                {
                    "id": "one",
                    "properties": {
                        "Played Seconds": {"number": 0},
                        "ASR Status": {"select": {"name": "待处理"}},
                    },
                }
            ]

        def update_page(self, page_id: str, payload: object) -> dict[str, object]:
            updates.append((page_id, payload))
            return {}

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "2",
                "--confirm",
                "ARCHIVE_2_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "exact preflight count changed" in error
    assert "no changes" in error
    assert updates == []


def test_archive_legacy_zero_play_refuses_missing_page_id(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        def query_data_source(self, _source: str) -> list[dict[str, object]]:
            return [{"properties": {"Played Seconds": {"number": 0}}}]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1",
                "--confirm",
                "ARCHIVE_1_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "missing stable IDs" in error
    assert "no changes" in error


def test_archive_legacy_zero_play_reports_incomplete_verification(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    page = {
        "id": "legacy-page",
        "properties": {"Played Seconds": {"number": 0}},
    }

    class FakeNotion(FakeContextClient):
        def query_data_source(self, _source: str) -> list[dict[str, object]]:
            return [page]

        def update_page(self, _page_id: str, _payload: object) -> dict[str, object]:
            return {"in_trash": True}

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1",
                "--confirm",
                "ARCHIVE_1_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 4
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Archive incomplete" in error
    assert "remaining=1" in error


def test_archive_legacy_zero_play_reports_notion_error(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeNotion(FakeContextClient):
        def query_data_source(self, _source: str) -> list[dict[str, object]]:
            return [{"id": "legacy", "properties": {"Played Seconds": {"number": 0}}}]

        def update_page(self, _page_id: str, _payload: object) -> dict[str, object]:
            raise cli_module.NotionAPIError("safe archive fixture failure")  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1",
                "--confirm",
                "ARCHIVE_1_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 4
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Notion archive error" in error
    assert "archived_before_failure=0" in error
    assert "safe archive fixture failure" in error


def test_archive_legacy_zero_play_reports_missing_resource(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1",
                "--confirm",
                "ARCHIVE_1_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 4
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Required Episode database was not found" in error


def test_archive_legacy_zero_play_reports_configuration_error(
    capsys: object,
    monkeypatch: object,
) -> None:
    def fail_runtime(_args: object) -> tuple[object, str]:
        raise cli_module.ConfigurationError("safe configuration fixture failure")  # type: ignore[attr-defined]

    monkeypatch.setattr(cli_module, "_notion_runtime", fail_runtime)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1",
                "--confirm",
                "ARCHIVE_1_LEGACY_ZERO_PLAY_EPISODES",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Configuration error" in error
    assert "safe configuration fixture failure" in error


def test_archive_legacy_zero_play_requires_bound_confirmation(
    capsys: object,
) -> None:
    assert (
        main(
            [
                "archive-legacy-zero-play",
                "--expected-count",
                "1142",
                "--confirm",
                "WRONG_CONFIRMATION",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "ARCHIVE_1142_LEGACY_ZERO_PLAY_EPISODES" in error


def test_notion_backlog_audit_reports_only_aggregate_counts(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    def episode(
        eid: str,
        *,
        played: int = 0,
        status: str = "待处理",
        playlist: bool = False,
        provider: str = "",
        model: str = "",
    ) -> dict[str, object]:
        return {
            "id": f"page-{eid}",
            "properties": {
                "Name": {"title": [{"plain_text": f"private-{eid}"}]},
                "EID": {"rich_text": [{"plain_text": eid}]},
                "Audio URL": {"url": f"https://audio.example/{eid}.mp3"},
                "Played Seconds": {"number": played},
                "Favorited": {"checkbox": False},
                "Liked": {"checkbox": False},
                "In Playlist": {"checkbox": playlist},
                "Skip AI": {"checkbox": False},
                "ASR Status": {"select": {"name": status}},
                "ASR Provider": {"rich_text": [{"plain_text": provider}]} if provider else {},
                "ASR Model": {"rich_text": [{"plain_text": model}]} if model else {},
            },
        }

    episodes = [
        episode(
            "normal",
            played=300,
            provider="legacy",
            model="legacy",
        ),
        episode("protected-zero", playlist=True),
        episode("legacy-zero"),
        episode(
            "final",
            played=300,
            status="最终失败",
            provider="siliconflow",
            model="FunAudioLLM/SenseVoiceSmall",
        ),
        episode("retry", played=300, status="可重试失败"),
    ]
    podcasts = [
        {
            "id": "external",
            "properties": {
                "Cover": {
                    "files": [
                        {
                            "type": "external",
                            "external": {"url": "https://image.example/cover.jpg"},
                        }
                    ]
                }
            },
        },
        {
            "id": "notion",
            "properties": {
                "Cover": {
                    "files": [
                        {
                            "type": "file",
                            "file": {"url": "https://notion.example/cover.jpg"},
                        }
                    ]
                }
            },
        },
        {"id": "missing", "properties": {"Cover": {"files": []}}},
    ]

    class FakeNotion(FakeContextClient):
        def query_data_source(self, source: str) -> list[dict[str, object]]:
            return episodes if source == "episodes" else podcasts

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def discover_existing_resources(self) -> dict[str, object]:
            return {
                "episode": SimpleNamespace(data_source_id="episodes"),
                "podcast": SimpleNamespace(data_source_id="podcasts"),
            }

    class FakeStore:
        def __init__(self, _api: object) -> None:
            pass

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def load(self, _page: object, eid: str) -> object:
            assert eid == "final"
            failure = ProviderFailure(
                provider="local_whisper",
                category=ProviderErrorCategory.UNSUPPORTED,
                message="safe fixture failure",
            )
            return SimpleNamespace(record=SimpleNamespace(failure=failure))

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", FakeStore)  # type: ignore[attr-defined]

    assert main(["audit-notion-backlog"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "normal_ai_candidates=1" in output
    assert "retry_ai_candidates=1" in output
    assert "statistics_total_seconds=0" in output
    assert "statistics_baseline=unset" in output
    assert "asr_providers: legacy=1, siliconflow=1" in output
    assert "asr_models: FunAudioLLM/SenseVoiceSmall=1, legacy=1" in output
    assert "tingwu_checkpoints" not in output
    assert "local_whisper:unsupported=1" in output
    assert "zero_play_total=2" in output
    assert "zero_play_protected=1" in output
    assert "legacy_zero_play=1" in output
    assert "external_covers=1" in output
    assert "notion_covers=1" in output
    assert "missing_covers=1" in output
    assert "private-" not in output
    assert "safe fixture failure" not in output


def test_notion_backlog_property_helpers_cover_malformed_values() -> None:
    assert cli_module._notion_property_text({}, "Missing") == ""  # type: ignore[attr-defined]
    assert cli_module._notion_property_text({"Value": {}}, "Value") == ""  # type: ignore[attr-defined]
    assert (  # type: ignore[attr-defined]
        cli_module._notion_property_text(
            {
                "Value": {
                    "rich_text": [
                        None,
                        {"text": {"content": "fallback"}},
                        {"text": {}},
                    ]
                }
            },
            "Value",
        )
        == "fallback"
    )
    assert cli_module._notion_property_number({}, "Missing") == 0  # type: ignore[attr-defined]
    assert cli_module._notion_property_number({"Value": {}}, "Value") == 0  # type: ignore[attr-defined]
    assert cli_module._notion_property_checkbox({}, "Missing") is False  # type: ignore[attr-defined]
    assert cli_module._cover_storage_kind({}) == "missing"  # type: ignore[attr-defined]
    assert cli_module._cover_storage_kind({"Cover": {}}) == "missing"  # type: ignore[attr-defined]
    assert cli_module._cover_storage_kind({"Cover": {"files": [None]}}) == "missing"  # type: ignore[attr-defined]
    schema_failure = ProviderFailure(
        provider="siliconflow_summary",
        category=ProviderErrorCategory.SCHEMA_CHANGED,
        message="SiliconFlow JSON repair did not satisfy the summary schema",
    )
    timeline_failure = schema_failure.model_copy(
        update={"message": "SiliconFlow JSON repair did not satisfy timeline constraints"}
    )
    assert cli_module._safe_failure_reason_code(schema_failure) == "summary_schema"  # type: ignore[attr-defined]
    assert cli_module._safe_failure_reason_code(timeline_failure) == "timeline_constraints"  # type: ignore[attr-defined]


def test_notion_backlog_audit_handles_unreadable_final_states(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    def final_page(eid_items: list[object]) -> dict[str, object]:
        return {
            "id": "final",
            "properties": {
                "EID": {"rich_text": eid_items},
                "ASR Status": {"select": {"name": "最终失败"}},
                "Played Seconds": {"number": 300},
            },
        }

    episodes = [
        final_page([]),
        final_page([{"plain_text": "load-error"}]),
        final_page([{"plain_text": "no-failure"}]),
        {"id": "invalid-properties", "properties": None},
    ]

    class FakeNotion(FakeContextClient):
        def query_data_source(self, source: str) -> list[dict[str, object]]:
            return episodes if source == "episodes" else [{"id": "invalid", "properties": None}]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {
                "episode": SimpleNamespace(data_source_id="episodes"),
                "podcast": SimpleNamespace(data_source_id="podcasts"),
            }

    class FakeStore:
        def __init__(self, _api: object) -> None:
            pass

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def load(self, _page: object, eid: str) -> object:
            if eid == "load-error":
                raise cli_module.NotionAPIError("safe fixture error")  # type: ignore[attr-defined]
            return SimpleNamespace(record=SimpleNamespace(failure=None))

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", FakeStore)  # type: ignore[attr-defined]

    assert main(["audit-notion-backlog"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "state_unreadable=2" in output
    assert "state_missing_failure=1" in output
    assert "safe fixture error" not in output


def test_notion_backlog_audit_reports_missing_resources(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    assert main(["audit-notion-backlog"]) == 4
    assert "Required Xyz2Notion databases were not found" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_notion_backlog_audit_reports_missing_credentials(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert main(["audit-notion-backlog"]) == 2
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_safe_failure_reason_distinguishes_summary_invalid_input() -> None:
    empty = ProviderFailure(
        provider="siliconflow_summary",
        category=ProviderErrorCategory.INVALID_INPUT,
        message="Transcript contains no readable content",
    )
    rejected = ProviderFailure(
        provider="siliconflow_summary",
        category=ProviderErrorCategory.INVALID_INPUT,
        message="SiliconFlow rejected the summary request (HTTP 400)",
        code="context_length_exceeded",
    )
    unsafe_code = rejected.model_copy(update={"code": "private code with spaces"})
    assert cli_module._safe_failure_reason_code(empty) == "empty_transcript"  # type: ignore[attr-defined]
    assert (  # type: ignore[attr-defined]
        cli_module._safe_failure_reason_code(rejected) == "request_http_400_context_length_exceeded"
    )
    assert cli_module._safe_failure_reason_code(unsafe_code) == "request_http_400"  # type: ignore[attr-defined]


def test_reopen_timeline_failures_requires_bound_confirmation(
    capsys: object,
) -> None:
    assert (
        main(
            [
                "reopen-timeline-failures",
                "--limit",
                "4",
                "--confirm",
                "wrong",
            ]
        )
        == 2
    )
    assert "REOPEN_4_TIMELINE_FAILURES" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert (
        main(
            [
                "reopen-summary-failures",
                "--limit",
                "1",
                "--confirm",
                "wrong",
            ]
        )
        == 2
    )
    assert "REOPEN_1_SUMMARY_FAILURES" in capsys.readouterr().err  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("category", "message", "code"),
    [
        (
            ProviderErrorCategory.SCHEMA_CHANGED,
            "SiliconFlow JSON repair did not satisfy the summary schema",
            None,
        ),
        (
            ProviderErrorCategory.INVALID_INPUT,
            "SiliconFlow rejected the summary request (HTTP 400)",
            "20015",
        ),
    ],
)
def test_reopen_summary_failure_preserves_transcript_checkpoint(
    capsys: object,
    monkeypatch: object,
    category: ProviderErrorCategory,
    message: str,
    code: str | None,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    failure = ProviderFailure(
        provider="siliconflow_summary",
        category=category,
        message=message,
        code=code,
    )
    record = PipelineRecord(eid="timeline").transition(PipelineState.TRANSCRIBED)
    record = record.transition(PipelineState.FAILED_FINAL, failure=failure)
    transcript = TranscriptResult(
        provider="local_whisper",
        provider_task_id="task",
        model="small",
        duration_ms=1_000,
        text="已有文字稿",
        segments=(TranscriptSegment(start_ms=0, end_ms=1_000, text="已有文字稿"),),
    )
    state = EpisodeAIState(record=record, transcript=transcript)
    pages = [
        {
            "id": "timeline-page",
            "properties": {
                "EID": {"rich_text": [{"plain_text": "timeline"}]},
            },
        }
    ]

    class FakeNotion(FakeContextClient):
        def query_data_source(
            self,
            source: str,
            payload: object,
        ) -> list[dict[str, object]]:
            assert source == "episodes"
            assert payload == {
                "page_size": 100,
                "filter": {
                    "property": "ASR Status",
                    "select": {"equals": "最终失败"},
                },
            }
            return pages

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    class FakeStore:
        saved: ClassVar[list[EpisodeAIState]] = []

        def __init__(self, _api: object) -> None:
            pass

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def load(self, _page: object, eid: str) -> EpisodeAIState:
            assert eid == "timeline"
            return state

        def save(self, page_id: str, updated: EpisodeAIState) -> EpisodeAIState:
            assert page_id == "timeline-page"
            self.saved.append(updated)
            return updated

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", FakeStore)  # type: ignore[attr-defined]
    FakeStore.saved = []

    assert (
        main(
            [
                "reopen-summary-failures",
                "--limit",
                "1",
                "--confirm",
                "REOPEN_1_SUMMARY_FAILURES",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "reopened=1" in output
    reopened = FakeStore.saved[0]
    assert reopened.record.state is PipelineState.TRANSCRIBED
    assert reopened.record.failure is None
    assert reopened.transcript == transcript
    assert reopened.summary is None


def test_reopen_timeline_failures_skips_unrelated_or_incomplete_states(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    unrelated_failure = ProviderFailure(
        provider="local_whisper",
        category=ProviderErrorCategory.UNSUPPORTED,
        message="safe unrelated failure",
    )
    timeline_failure = ProviderFailure(
        provider="siliconflow_summary",
        category=ProviderErrorCategory.SCHEMA_CHANGED,
        message="SiliconFlow JSON repair did not satisfy timeline constraints",
    )
    unrelated = EpisodeAIState(
        record=PipelineRecord(eid="unrelated").transition(
            PipelineState.FAILED_FINAL,
            failure=unrelated_failure,
        )
    )
    no_transcript = EpisodeAIState(
        record=PipelineRecord(eid="no-transcript").transition(
            PipelineState.FAILED_FINAL,
            failure=timeline_failure,
        )
    )
    pages = [
        {"id": "invalid", "properties": None},
        {"id": "missing-eid", "properties": {}},
        {
            "id": "unrelated",
            "properties": {"EID": {"rich_text": [{"plain_text": "unrelated"}]}},
        },
        {
            "id": "no-transcript",
            "properties": {"EID": {"rich_text": [{"plain_text": "no-transcript"}]}},
        },
    ]

    class FakeNotion(FakeContextClient):
        def query_data_source(self, _source: str, _payload: object) -> list[dict[str, object]]:
            return pages

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": SimpleNamespace(data_source_id="episodes")}

    class FakeStore:
        def __init__(self, _api: object) -> None:
            pass

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def load(self, _page: object, eid: str) -> EpisodeAIState:
            return unrelated if eid == "unrelated" else no_transcript

        def save(self, _page_id: str, _state: EpisodeAIState) -> EpisodeAIState:
            raise AssertionError("unrelated failures must not be reopened")

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", FakeStore)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "reopen-timeline-failures",
                "--limit",
                "4",
                "--confirm",
                "REOPEN_4_TIMELINE_FAILURES",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "reopened=0" in output
    assert "skipped=4" in output
    assert "safe unrelated failure" not in output


def test_reopen_timeline_failures_requires_episode_database(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret_fixture_token")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def discover_existing_resources(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    assert (
        main(
            [
                "reopen-timeline-failures",
                "--limit",
                "4",
                "--confirm",
                "REOPEN_4_TIMELINE_FAILURES",
            ]
        )
        == 4
    )
    assert "Required Episode database was not found" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_migration_dry_run_reports_only_counts(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def discover_existing_resources(self) -> dict[str, object]:
            return {"episode": object()}

    class FakeMigrator:
        def __init__(self, _api: object, resources: object, page_id: str) -> None:
            assert resources == {"episode": resources["episode"]}  # type: ignore[index]
            assert page_id == "fixture-page"

        def migrate(self, *, dry_run: bool) -> object:
            assert dry_run is True
            return SimpleNamespace(
                scanned_pages=7,
                planned_updates=4,
                updated_pages=0,
                legacy_embeds_found=2,
                legacy_embeds_removed=0,
                duplicate_keys=(),
                dry_run=True,
            )

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "LegacyTemplateMigrator", FakeMigrator)  # type: ignore[attr-defined]
    assert main(["migrate", "--dry-run"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "scanned=7" in output
    assert "planned=4" in output


def test_migration_apply_reports_aggregate_result(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def initialize(self) -> object:
            return SimpleNamespace(resources={"episode": object()})

    class FakeMigrator:
        def __init__(self, _api: object, _resources: object, _page_id: str) -> None:
            pass

        def migrate(self, *, dry_run: bool) -> object:
            assert dry_run is False
            return SimpleNamespace(
                scanned_pages=8,
                planned_updates=5,
                updated_pages=5,
                legacy_embeds_found=3,
                legacy_embeds_removed=3,
                duplicate_keys=(),
                dry_run=False,
            )

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "LegacyTemplateMigrator", FakeMigrator)  # type: ignore[attr-defined]
    assert main(["migrate"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Migration complete" in output
    assert "updated=5" in output
    assert "removed=3" in output


def test_redo_episode_success_does_not_print_eid(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def initialize(self) -> object:
            return SimpleNamespace(
                resources={
                    "episode": SimpleNamespace(data_source_id="episode-source"),
                }
            )

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "reset_episode_ai",
        lambda _api, source, eid: calls.append((source, eid)),
    )
    assert main(["redo-episode", "--eid", "private-eid"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert calls == [("episode-source", "private-eid")]
    assert output.strip() == "Episode AI state reset OK (count=1)"
    assert "private-eid" not in output


def test_rebuild_statistics_and_heatmap_require_only_notion_credentials(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["rebuild-statistics"]) == 2
    assert "notion_token" in capsys.readouterr().err  # type: ignore[attr-defined]
    assert main(["rebuild-heatmap"]) == 2
    assert "notion_token" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_rebuild_statistics_runs_notion_only_reconciliation(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self) -> object:
            return SimpleNamespace(resources={"safe": "resources"})

    class FakeStatistics:
        def __init__(
            self,
            _api: object,
            resources: object,
            *,
            root_page_id: str,
        ) -> None:
            assert resources == {"safe": "resources"}
            assert root_page_id == "fixture-page"

        def sync(self) -> object:
            return SimpleNamespace(
                mode="incremental",
                baseline_episodes=0,
                ledger_episodes=2,
                delta_seconds=300,
                total_seconds=522_300,
                daily=(),
            )

    class FakeHeatmap:
        def __init__(self, _api: object, root_page_id: str) -> None:
            assert root_page_id == "fixture-page"

        def publish(self, _year: int, daily: object) -> object:
            assert daily == ()
            return SimpleNamespace(action="unchanged")

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "NotionIncrementalStatistics",
        FakeStatistics,
    )
    monkeypatch.setattr(cli_module, "HeatmapPublisher", FakeHeatmap)  # type: ignore[attr-defined]

    assert main(["rebuild-statistics"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Notion-only statistics reconciliation OK" in output
    assert "delta_seconds=300" in output
    assert "total_seconds=522300" in output
    assert "heatmap=unchanged" in output
