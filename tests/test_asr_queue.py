from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import xyz2notion.cli as cli_module
from xyz2notion.asr.tingwu import (
    RECORD_LIST_URL,
    TingwuClient,
    TingwuTask,
    TingwuTaskState,
)
from xyz2notion.cli import ASR_INTER_EPISODE_SECONDS, build_parser, main
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    EpisodeCandidate,
    ProcessingOutcome,
)
from xyz2notion.orchestration.state_store import EpisodeAIState
from xyz2notion.state import PipelineRecord, PipelineState


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "code": "0", "data": data})


def _tingwu_client(handler: Any) -> TingwuClient:
    return TingwuClient(
        "session=fixture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


class _StateStore:
    def __init__(self, state: EpisodeAIState) -> None:
        self.state = state
        self.saved: list[EpisodeAIState] = []

    def load(self, _page: Mapping[str, Any], _eid: str) -> EpisodeAIState:
        return self.state

    def save(self, _page_id: str, state: EpisodeAIState) -> EpisodeAIState:
        self.state = state
        self.saved.append(state)
        return state


class _ResumeOnlyTingwu:
    def __init__(self, task: TingwuTask) -> None:
        self.task = task
        self.resume_calls = 0
        self.submit_calls = 0

    def resume_episode(self, *_args: object, **_kwargs: object) -> TingwuTask:
        self.resume_calls += 1
        return self.task

    def submit_episode(self, *_args: object, **_kwargs: object) -> TingwuTask:
        self.submit_calls += 1
        raise AssertionError("an in-flight checkpoint must never be submitted again")


class _TranscriptProcessor(EpisodeAIProcessor):
    def _siliconflow(self, _candidate: EpisodeCandidate) -> TranscriptResult:
        return TranscriptResult(
            provider="siliconflow",
            provider_task_id="task",
            model="FunAudioLLM/SenseVoiceSmall",
            duration_ms=1_000,
            text="文字稿",
            segments=(TranscriptSegment(start_ms=0, end_ms=1_000, text="文字稿"),),
            timing_quality=TranscriptTimingQuality.EXACT,
        )


class _AmbiguousTingwu:
    def resume_episode(self, *_args: object, **_kwargs: object) -> TingwuTask:
        raise ProviderError(
            ProviderFailure(
                provider="tingwu_cookie",
                category=ProviderErrorCategory.UNAVAILABLE,
                message="Tingwu returned multiple matching records; manual review is required",
                code="ambiguous_record",
            )
        )

    def submit_episode(self, *_args: object, **_kwargs: object) -> TingwuTask:
        raise AssertionError("an in-flight checkpoint must never be submitted again")


class _FallbackGuardProcessor(EpisodeAIProcessor):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fallback_calls = 0

    def _siliconflow(self, _candidate: EpisodeCandidate) -> TranscriptResult:
        self.fallback_calls += 1
        raise AssertionError("an in-flight Tingwu task must never fall back")


CANDIDATE = EpisodeCandidate("page", "episode", "标题", "https://cdn.example/audio")


def _episode_page(
    page_id: str,
    eid: str,
    title: str,
    *,
    status: str,
) -> dict[str, object]:
    return {
        "id": page_id,
        "properties": {
            "EID": {"rich_text": [{"plain_text": eid}]},
            "Name": {"title": [{"plain_text": title}]},
            "Audio URL": {"url": f"https://cdn.example/{eid}.mp3"},
            "Played Seconds": {"number": 120},
            "ASR Status": {"select": {"name": status}},
        },
    }


class _ContextProvider:
    def __enter__(self) -> _ContextProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


class _CLIFakeStore(_ContextProvider):
    def __init__(self, _api: object) -> None:
        pass


class _CLIFakeInitializer:
    def __init__(self, _api: object, page_id: str) -> None:
        assert page_id == "fixture-page"

    def initialize(self) -> object:
        return SimpleNamespace(
            resources={"episode": SimpleNamespace(data_source_id="episode-source")}
        )


def test_resume_without_visible_record_never_resubmits() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert str(request.url) == RECORD_LIST_URL
        return _ok({"batchRecord": []})

    task = _tingwu_client(handler).resume_episode(
        "directory",
        "标题",
        provider_task_id="persisted-task",
        source_task_id="persisted-source",
    )

    assert task.state is TingwuTaskState.SUBMITTED
    assert task.provider_task_id == "persisted-task"
    assert seen_urls == [RECORD_LIST_URL]


def test_ambiguous_records_stop_without_creating_a_third_record() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        payload = json.loads(request.content)
        assert payload["filter"]["showName"] == "标题"
        return _ok(
            {
                "batchRecord": [
                    {
                        "recordList": [
                            {"genRecordId": "first", "status": 1, "showName": "标题"},
                            {"genRecordId": "second", "status": 1, "showName": "标题"},
                        ]
                    }
                ]
            }
        )

    with pytest.raises(ProviderError) as caught:
        _tingwu_client(handler).resume_episode(
            "directory",
            "标题",
            provider_task_id="source-id",
            source_task_id="source-id",
        )

    assert caught.value.failure.code == "ambiguous_record"
    assert seen_urls == [RECORD_LIST_URL]


def test_unique_persisted_record_id_resolves_duplicate_titles_safely() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _ok(
            [
                {"transId": "older-record", "status": 1, "showName": "标题"},
                {"transId": "persisted-record", "status": 0, "showName": "标题"},
            ]
        )

    client = _tingwu_client(handler)
    task = client.resume_episode(
        "directory",
        "标题",
        provider_task_id="persisted-record",
        source_task_id="source-id",
    )
    assert task.provider_task_id == "persisted-record"
    assert task.state is TingwuTaskState.SUCCEEDED

    with pytest.raises(ProviderError) as caught:
        client.find_record("directory", "标题")
    assert caught.value.failure.code == "ambiguous_record"


def test_unconfirmed_source_task_id_without_record_is_not_treated_as_submitted() -> None:
    client = _tingwu_client(lambda _request: _ok([]))

    with pytest.raises(ProviderError) as caught:
        client.resume_episode(
            "directory",
            "标题",
            provider_task_id="source-id",
            source_task_id="source-id",
        )

    assert caught.value.failure.category is ProviderErrorCategory.UNAVAILABLE
    assert caught.value.failure.code == "record_not_visible"


def test_in_flight_processor_uses_read_only_resume_path() -> None:
    record = PipelineRecord(eid="episode").transition(PipelineState.ASR_SUBMITTED)
    store = _StateStore(
        EpisodeAIState(
            record=record,
            provider="tingwu_cookie",
            provider_task_id="persisted-task",
            source_task_id="persisted-source",
            tingwu_directory_id="directory",
            tingwu_title="标题",
        )
    )
    tingwu = _ResumeOnlyTingwu(
        TingwuTask(
            provider_task_id="persisted-task",
            source_task_id="persisted-source",
            state=TingwuTaskState.PROCESSING,
            directory_id="directory",
            title="标题",
        )
    )

    outcome = EpisodeAIProcessor(
        object(),
        store,  # type: ignore[arg-type]
        tingwu=tingwu,  # type: ignore[arg-type]
    ).process_asr_only(CANDIDATE, {})

    assert outcome.state is PipelineState.ASR_RUNNING
    assert tingwu.resume_calls == 1
    assert tingwu.submit_calls == 0


def test_ambiguous_in_flight_record_stops_without_submit_or_fallback() -> None:
    record = PipelineRecord(eid="episode").transition(PipelineState.ASR_SUBMITTED)
    store = _StateStore(
        EpisodeAIState(
            record=record,
            provider="tingwu_cookie",
            provider_task_id="source-id",
            source_task_id="source-id",
            tingwu_directory_id="directory",
            tingwu_title="标题",
        )
    )
    processor = _FallbackGuardProcessor(
        object(),
        store,
        tingwu=_AmbiguousTingwu(),  # type: ignore[arg-type]
        siliconflow=object(),  # type: ignore[arg-type]
    )

    outcome = processor.process_asr_only(CANDIDATE, {})

    assert outcome.action == "failed"
    assert outcome.state is PipelineState.FAILED_RETRYABLE
    assert processor.fallback_calls == 0
    assert store.state.record.failure is not None
    assert store.state.record.failure.code == "ambiguous_record"


def test_incomplete_in_flight_checkpoint_fails_without_network_or_fallback() -> None:
    record = PipelineRecord(eid="episode").transition(PipelineState.ASR_SUBMITTED)
    store = _StateStore(
        EpisodeAIState(
            record=record,
            provider="tingwu_cookie",
            provider_task_id="persisted-task",
        )
    )
    processor = _FallbackGuardProcessor(
        object(),
        store,
        tingwu=_AmbiguousTingwu(),  # type: ignore[arg-type]
        siliconflow=object(),  # type: ignore[arg-type]
    )
    outcome = processor.process_asr_only(CANDIDATE, {})
    assert outcome.state is PipelineState.FAILED_FINAL
    assert processor.fallback_calls == 0
    assert store.state.record.failure is not None
    assert store.state.record.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_asr_only_processor_stops_at_transcribed() -> None:
    store = _StateStore(EpisodeAIState(record=PipelineRecord(eid="episode")))
    outcome = _TranscriptProcessor(
        object(),
        store,  # type: ignore[arg-type]
        siliconflow=object(),  # type: ignore[arg-type]
        summary_client=object(),  # type: ignore[arg-type]
    ).process_asr_only(CANDIDATE, {})

    assert outcome.action == "transcribed"
    assert outcome.state is PipelineState.TRANSCRIBED
    assert store.state.summary is None
    assert [state.record.state for state in store.saved] == [PipelineState.TRANSCRIBED]


def test_asr_only_processor_pauses_without_any_provider() -> None:
    store = _StateStore(EpisodeAIState(record=PipelineRecord(eid="episode")))
    outcome = EpisodeAIProcessor(object(), store).process_asr_only(CANDIDATE, {})
    assert outcome.action == "paused"
    assert outcome.state is PipelineState.DISCOVERED
    assert store.saved == []


def test_process_asr_cli_enforces_bounded_modes_and_limits() -> None:
    parser = build_parser()
    args = parser.parse_args(["process-asr", "--mode", "backlog", "--limit", "2"])
    assert args.mode == "backlog"
    assert args.limit == 2
    assert ASR_INTER_EPISODE_SECONDS == 60
    with pytest.raises(SystemExit):
        parser.parse_args(["process-asr", "--limit", "3"])


def test_process_asr_cli_runs_two_sequential_candidates_with_safe_aggregate_output(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setenv("TINGWU_COOKIE", "fixture-cookie")  # type: ignore[attr-defined]
    monkeypatch.setenv("SILICONFLOW_API_KEY", "fixture-key")  # type: ignore[attr-defined]
    pages = [
        _episode_page("page-running", "private-eid-running", "private title 1", status="转写中"),
        _episode_page("page-new", "private-eid-new", "private title 2", status="待处理"),
    ]
    sleeps: list[float] = []
    processed: list[str] = []

    class FakeNotion(_ContextProvider):
        def __init__(self, _token: object) -> None:
            pass

        def query_data_source(
            self,
            data_source_id: str,
            payload: object,
        ) -> list[dict[str, object]]:
            assert data_source_id == "episode-source"
            assert payload == {"page_size": 100}
            return pages

    class FakeProcessor:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            assert kwargs["summary_enabled"] is False
            assert "summary_client" not in kwargs

        def process_asr_only(
            self,
            candidate: EpisodeCandidate,
            candidate_page: object,
        ) -> ProcessingOutcome:
            assert candidate_page in pages
            processed.append(candidate.page_id)
            if candidate.page_id == "page-running":
                return ProcessingOutcome(
                    candidate.eid,
                    "pending",
                    PipelineState.ASR_RUNNING,
                )
            return ProcessingOutcome(
                candidate.eid,
                "transcribed",
                PipelineState.TRANSCRIBED,
            )

    providers = (_ContextProvider(), _ContextProvider(), _ContextProvider(), None)
    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", _CLIFakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", _CLIFakeStore)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "EpisodeAIProcessor", FakeProcessor)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "build_provider_clients", lambda **_kwargs: providers)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module.time, "sleep", sleeps.append)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "process-asr",
                "--config",
                "config.example.yaml",
                "--mode",
                "backlog",
                "--limit",
                "2",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert processed == ["page-running", "page-new"]
    assert sleeps == [60]
    assert "mode=backlog" in output
    assert "selected=2" in output
    assert "remaining=1" in output
    assert "interval_seconds=60" in output
    assert "pending=1" in output
    assert "transcribed=1" in output
    assert "ASR_RUNNING=1" in output
    assert "TRANSCRIBED=1" in output
    assert "private title" not in output
    assert "private-eid" not in output


def test_process_asr_cli_reports_missing_config(
    capsys: object,
) -> None:
    assert main(["process-asr", "--config", "missing-asr-config.yaml"]) == 2
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_process_asr_cli_reports_missing_target_page(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert main(["process-asr", "--config", "config.example.yaml"]) == 2
    assert "Missing target page" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_process_asr_cli_reports_notion_error(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]

    class FailingNotion(_ContextProvider):
        def __init__(self, _token: object) -> None:
            pass

        def query_data_source(
            self,
            _data_source_id: str,
            _payload: object,
        ) -> list[dict[str, object]]:
            raise cli_module.NotionAPIError("safe fixture failure")  # type: ignore[attr-defined]

    monkeypatch.setattr(cli_module, "NotionClient", FailingNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", _CLIFakeInitializer)  # type: ignore[attr-defined]

    assert main(["process-asr", "--config", "config.example.yaml"]) == 4
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Notion error: safe fixture failure" in error


def test_asr_workflow_is_guarded_bounded_and_sequential() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "transcribe-asr.yml"
    ).read_text()
    assert "vars.ASR_QUEUE_ENABLED == 'true'" in workflow
    assert "vars.ASR_BACKFILL_ACTIVE == 'true'" in workflow
    assert 'cron: "13 */2 * * *"' in workflow
    assert 'cron: "47 21 * * *"' in workflow
    assert 'default: "2"' in workflow
    assert '--mode "$mode" --limit "$limit"' in workflow
    assert "strategy:" not in workflow
    assert "max-parallel" not in workflow
    assert "~/.cache/huggingface" in workflow
    assert "actions: write" not in workflow
    assert "gh variable set" not in workflow
