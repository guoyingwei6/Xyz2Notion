from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

import xyz2notion.cli as cli_module
from xyz2notion.cli import ASR_INTER_EPISODE_SECONDS, build_parser, main
from xyz2notion.config import AppConfig, LimitConfig
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


CANDIDATE = EpisodeCandidate("page", "episode", "标题", "https://cdn.example/audio")


def _episode_page(page_id: str, eid: str, title: str, *, status: str) -> dict[str, object]:
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


def test_inflight_dashscope_checkpoint_is_polled_without_resubmission() -> None:
    record = (
        PipelineRecord(eid="episode")
        .transition(PipelineState.ASR_SUBMITTED)
        .transition(PipelineState.ASR_RUNNING)
    )
    store = _StateStore(
        EpisodeAIState(
            record=record,
            provider="dashscope",
            provider_task_id="existing-task",
        )
    )
    calls: list[str] = []

    class ExistingTask:
        def wait_result_url(self, task_id: str) -> str:
            calls.append(f"poll:{task_id}")
            return "https://dashscope.example/result.json"

        def fetch_transcript(self, url: str, *, task_id: str) -> TranscriptResult:
            calls.append(f"fetch:{task_id}:{url}")
            return TranscriptResult(
                provider="dashscope",
                provider_task_id=task_id,
                model="paraformer-v1",
                duration_ms=1_000,
                text="文字稿",
                segments=(TranscriptSegment(start_ms=0, end_ms=1_000, text="文字稿"),),
                timing_quality=TranscriptTimingQuality.EXACT,
            )

    outcome = EpisodeAIProcessor(
        object(),
        store,
        dashscope=ExistingTask(),  # type: ignore[arg-type]
    ).process(CANDIDATE, {})

    assert outcome.action == "waiting_summary_key"
    assert outcome.state is PipelineState.TRANSCRIBED
    assert calls == [
        "poll:existing-task",
        "fetch:existing-task:https://dashscope.example/result.json",
    ]
    assert store.state.record.state is PipelineState.TRANSCRIBED


def test_asr_only_retry_resumes_only_the_failed_asr_checkpoint() -> None:
    store = _StateStore(EpisodeAIState(record=PipelineRecord(eid="episode")))

    class FlakyProcessor(_TranscriptProcessor):
        calls = 0

        def _siliconflow(self, _candidate: EpisodeCandidate) -> TranscriptResult:
            self.calls += 1
            if self.calls == 1:
                raise ProviderError(
                    ProviderFailure(
                        provider="siliconflow",
                        category=ProviderErrorCategory.RATE_LIMITED,
                        message="retry",
                    )
                )
            return super()._siliconflow(_candidate)

    processor = FlakyProcessor(
        object(),
        store,  # type: ignore[arg-type]
        siliconflow=object(),  # type: ignore[arg-type]
    )
    failed = processor.process_asr_only(CANDIDATE, {})
    assert failed.state is PipelineState.FAILED_RETRYABLE
    completed = processor.process_asr_only(CANDIDATE, {}, retry_failed=True)
    assert completed.action == "transcribed"
    assert completed.state is PipelineState.TRANSCRIBED

    # A retryable enrichment failure must not be consumed by the ASR-only lane.
    # Build that state explicitly and verify the row remains retryable.
    failed_record = PipelineRecord(eid="episode").transition(PipelineState.TRANSCRIBED)
    failed_record = failed_record.transition(
        PipelineState.FAILED_RETRYABLE,
        failure=ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.RATE_LIMITED,
            message="retry",
        ),
    )
    store.state = EpisodeAIState(record=failed_record)
    skipped = processor.process_asr_only(CANDIDATE, {}, retry_failed=True)
    assert skipped.action == "skipped"
    assert skipped.state is PipelineState.FAILED_RETRYABLE
    assert store.state.record.state is PipelineState.FAILED_RETRYABLE


def test_process_asr_cli_enforces_bounded_modes_and_limits() -> None:
    parser = build_parser()
    args = parser.parse_args(["process-asr", "--mode", "backlog", "--limit", "2"])
    assert args.mode == "backlog"
    assert args.limit == 2
    retry_args = parser.parse_args(["process-asr", "--mode", "retry", "--limit", "2"])
    assert retry_args.mode == "retry"
    assert ASR_INTER_EPISODE_SECONDS == 60
    with pytest.raises(SystemExit):
        parser.parse_args(["process-asr", "--limit", "3"])


@pytest.mark.parametrize("failure", [False, True])
def test_process_asr_cli_runs_two_sequential_candidates_with_safe_aggregate_output(
    capsys: object,
    monkeypatch: object,
    failure: bool,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.setenv("NOTION_PAGE_ID", "fixture-page")  # type: ignore[attr-defined]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-dashscope")  # type: ignore[attr-defined]
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
            *,
            retry_failed: bool = False,
        ) -> ProcessingOutcome:
            assert candidate_page in pages
            assert retry_failed is False
            processed.append(candidate.page_id)
            if failure:
                return ProcessingOutcome(candidate.eid, "failed", PipelineState.FAILED_FINAL)
            if candidate.page_id == "page-running":
                return ProcessingOutcome(candidate.eid, "pending", PipelineState.ASR_RUNNING)
            return ProcessingOutcome(candidate.eid, "transcribed", PipelineState.TRANSCRIBED)

    providers = (_ContextProvider(), _ContextProvider(), _ContextProvider(), None)
    monkeypatch.setattr(cli_module, "NotionClient", FakeNotion)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionInitializer", _CLIFakeInitializer)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "NotionEpisodeStateStore", _CLIFakeStore)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "EpisodeAIProcessor", FakeProcessor)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module, "build_provider_clients", lambda **_kwargs: providers)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_module.time, "sleep", sleeps.append)  # type: ignore[attr-defined]

    assert main(
        [
            "process-asr",
            "--config",
            "config.example.yaml",
            "--mode",
            "backlog",
            "--limit",
            "2",
        ]
    ) == (5 if failure else 0)
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert processed == ["page-running", "page-new"]
    assert sleeps == [60]
    assert "mode=backlog" in output
    assert "selected=2" in output
    assert f"remaining={0 if failure else 1}" in output
    assert "interval_seconds=60" in output
    if failure:
        assert "queue FAILED" in output
        assert "failed=2" in output
    else:
        assert "pending=1" in output
        assert "transcribed=1" in output
        assert "ASR_RUNNING=1" in output
        assert "TRANSCRIBED=1" in output
    assert "providers: unknown=2" in output
    assert "private title" not in output
    assert "private-eid" not in output
    processed.clear()
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_module,
        "load_config",
        lambda _path: AppConfig(limits=LimitConfig(episodes_per_run=1)),
    )
    main(["process-asr", "--config", "config.example.yaml", "--limit", "2"])
    assert len(processed) == 1


def test_process_asr_cli_reports_missing_config(capsys: object) -> None:
    assert main(["process-asr", "--config", "missing-asr-config.yaml"]) == 2
    assert "Configuration error" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_process_asr_cli_requires_target_page(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "fixture-notion")  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert main(["process-asr", "--config", "config.example.yaml"]) == 2
    assert "Missing target page" in capsys.readouterr().err  # type: ignore[attr-defined]
