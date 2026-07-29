from types import SimpleNamespace

import xyz2notion.cli as cli_module
from xyz2notion import __version__
from xyz2notion.cli import main
from xyz2notion.orchestration.processor import ProcessingOutcome
from xyz2notion.state import PipelineState


def test_doctor_reports_installation(capsys: object) -> None:
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert f"Xyz2Notion {__version__}: OK" in output
    assert "5 credential types" in output


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


class FakeContextClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "FakeContextClient":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def profile(self) -> dict[str, str]:
        return {"uid": "never-printed"}


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

    class FakeInitializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "fixture-page"

        def initialize(self) -> object:
            return SimpleNamespace(resources={})

    class FakeSynchronizer:
        def __init__(self, _api: object, resources: object) -> None:
            assert resources == {}

        def sync(self, snapshot: object) -> object:
            assert snapshot == "fixture-snapshot"
            return SimpleNamespace(created=2, updated=1, unchanged=3)

    class FakeStatisticsSynchronizer:
        def __init__(self, _api: object, resources: object) -> None:
            assert resources == {}

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
        lambda _api: "fixture-snapshot",
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

        def initialize(self) -> object:
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
        lambda **_kwargs: (None, None, None),
    )

    assert main(["process-ai", "--config", "config.example.yaml"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "selected=1" in output
    assert "pending=1" in output
    assert "ASR_RUNNING=1" in output
    assert "private episode title" not in output
    assert "private-eid" not in output
