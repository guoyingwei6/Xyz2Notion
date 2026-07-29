from types import SimpleNamespace
from typing import ClassVar

import xyz2notion.cli as cli_module
from xyz2notion import __version__
from xyz2notion.cli import main
from xyz2notion.orchestration.processor import ProcessingOutcome
from xyz2notion.state import PipelineState


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
    assert "tingwu_cookie, siliconflow" in output


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


def test_process_ai_reports_missing_notion_token(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    assert main(["process-ai", "--config", "config.example.yaml"]) == 2
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

    class FakeStatisticsSynchronizer:
        def __init__(
            self,
            _api: object,
            resources: object,
            root_page_id: str | None = None,
        ) -> None:
            assert resources == {}
            assert root_page_id == "fixture-page"

        def sync(self, statistics: object) -> object:
            assert statistics.marker == "fixture-statistics"  # type: ignore[attr-defined]
            return SimpleNamespace(created=4, updated=5, unchanged=6)

    class FakeHeatmapPublisher:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def publish(self, _year: int, daily: object) -> object:
            assert daily == "fixture-daily"
            return SimpleNamespace(action="updated")

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "MetadataSynchronizer", FakeSynchronizer)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "StatisticsSynchronizer",
        FakeStatisticsSynchronizer,
    )
    monkeypatch.setattr(cli_module, "HeatmapPublisher", FakeHeatmapPublisher)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "collect_metadata",
        lambda _api, **_kwargs: fixture_snapshot,
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "collect_monthly_wrapped",
        lambda _api, _snapshot: (),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "calculate_statistics",
        lambda _snapshot, _wrapped: SimpleNamespace(
            marker="fixture-statistics",
            daily="fixture-daily",
        ),
    )
    assert main(["sync-metadata"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "created: 2, updated: 1, unchanged: 3" in output
    assert "statistics created: 4, statistics updated: 5" in output
    assert "episodes played: 1, playlist: 2, favorites: 1" in output
    assert "heatmap: updated" in output


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
        "image",
        *(["child_database"] * 8),
        "divider",
        "callout",
    ]

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


def test_process_ai_reports_only_aggregate_counts(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    page = {
        "id": "episode-page",
        "properties": {
            "Name": {"title": [{"plain_text": "private episode title"}]},
            "EID": {"rich_text": [{"plain_text": "private-eid"}]},
            "Audio URL": {"url": "https://example.com/audio.mp3"},
            "Played Seconds": {"number": 120},
        },
    }

    class FakeNotion(FakeContextClient):
        def query_data_source(
            self,
            data_source_id: str,
            _payload: object,
        ) -> list[object]:
            assert data_source_id == "episode-source"
            return [page]

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self) -> object:
            return SimpleNamespace(
                resources={
                    "episode": SimpleNamespace(data_source_id="episode-source"),
                }
            )

    class FakeStore:
        def __init__(self, _api: object) -> None:
            pass

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class FakeProcessor:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def process(
            self,
            candidate: object,
            candidate_page: object,
            *,
            retry_failed: bool,
            only_failed: bool,
        ) -> ProcessingOutcome:
            assert candidate_page == page
            assert retry_failed is False
            assert only_failed is False
            return ProcessingOutcome(
                candidate.eid,  # type: ignore[attr-defined]
                "pending",
                PipelineState.ASR_RUNNING,
            )

    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", FakeStore)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "EpisodeAIProcessor", FakeProcessor)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "build_provider_clients",
        lambda **_kwargs: (None, None, None, None),
    )

    assert main(["process-ai", "--config", "config.example.yaml"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "selected=1" in output
    assert "pending=1" in output
    assert "ASR_RUNNING=1" in output
    assert "private episode title" not in output
    assert "private-eid" not in output


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


def _install_rebuild_fakes(monkeypatch: object) -> None:
    monkeypatch.setenv("XIAOYUZHOU_REFRESH_TOKEN", "fixture-refresh")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "XiaoyuzhouClient", FakeContextClient)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionClient", FakeContextClient)  # type: ignore[attr-defined]

    class FakeInitializer:
        def __init__(self, _api: object, _page_id: str) -> None:
            pass

        def initialize(self) -> object:
            return SimpleNamespace(resources={"year": object()})

    monkeypatch.setattr(cli_module, "NotionInitializer", FakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "collect_metadata", lambda _api: "snapshot")  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "collect_monthly_wrapped",
        lambda _api, _snapshot: "wrapped",
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "calculate_statistics",
        lambda _snapshot, _wrapped: SimpleNamespace(daily="daily"),
    )


def test_rebuild_statistics_success(capsys: object, monkeypatch: object) -> None:
    _install_rebuild_fakes(monkeypatch)

    class FakeSynchronizer:
        def __init__(
            self,
            _api: object,
            resources: object,
            root_page_id: str | None = None,
        ) -> None:
            assert resources == {"year": resources["year"]}  # type: ignore[index]
            assert root_page_id == "fixture-page"

        def sync(self, _statistics: object) -> object:
            return SimpleNamespace(created=2, updated=3, unchanged=4)

    monkeypatch.setattr(cli_module, "StatisticsSynchronizer", FakeSynchronizer)  # type: ignore[attr-defined]
    assert main(["rebuild-statistics"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "created=2, updated=3, unchanged=4" in output


def test_rebuild_heatmap_success(capsys: object, monkeypatch: object) -> None:
    _install_rebuild_fakes(monkeypatch)

    class FakeHeatmap:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def publish(self, _year: int, daily: object) -> object:
            assert daily == "daily"
            return SimpleNamespace(action="updated")

    monkeypatch.setattr(cli_module, "HeatmapPublisher", FakeHeatmap)  # type: ignore[attr-defined]
    assert main(["rebuild-heatmap"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert output.strip() == "Heatmap rebuild OK (action=updated)"
