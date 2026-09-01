from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from xyz2notion.config import (
    AppConfig,
    ConfigurationError,
    MissingCredentialError,
    RuntimeCredentials,
    SummaryConfig,
)
from xyz2notion.models import (
    MindmapNode,
    ProviderErrorCategory,
    ProviderFailure,
    SummaryResult,
    TranscriptResult,
)
from xyz2notion.notion.client import NotionAPIError
from xyz2notion.orchestration import manual_retry_queue as queue_module
from xyz2notion.orchestration.manual_retry_queue import (
    MANUAL_RETRY_INTER_EPISODE_SECONDS,
    MANUAL_RETRY_LIMIT,
    ManualRetryQueueResult,
    _limit,
    manual_reopen_target,
    manual_retry_requested,
    select_manual_retry_work,
)
from xyz2notion.orchestration.processor import EpisodeCandidate, ProcessingOutcome
from xyz2notion.orchestration.state_store import EpisodeAIState
from xyz2notion.state import PipelineRecord, PipelineState


def episode(
    page_id: str,
    *,
    favorite: bool = False,
    liked: bool = False,
    played_seconds: int = 0,
) -> dict[str, object]:
    return {
        "id": page_id,
        "properties": {
            "Name": {"title": [{"plain_text": page_id}]},
            "EID": {"rich_text": [{"plain_text": page_id}]},
            "Audio URL": {"url": f"https://audio.example/{page_id}.mp3"},
            "Played Seconds": {"number": played_seconds},
            "Favorited": {"checkbox": favorite},
            "Liked": {"checkbox": liked},
            "Listening Status": {"select": {"name": "未听"}},
            "人工请求重试": {"checkbox": True},
            "ASR Status": {"select": {"name": "最终失败"}},
        },
    }


def normal_state(eid: str, target: PipelineState) -> EpisodeAIState:
    record = PipelineRecord(eid=eid)
    path = {
        PipelineState.DISCOVERED: (),
        PipelineState.ASR_SUBMITTED: (PipelineState.ASR_SUBMITTED,),
        PipelineState.ASR_RUNNING: (
            PipelineState.ASR_SUBMITTED,
            PipelineState.ASR_RUNNING,
        ),
        PipelineState.TRANSCRIBED: (
            PipelineState.ASR_SUBMITTED,
            PipelineState.ASR_RUNNING,
            PipelineState.TRANSCRIBED,
        ),
        PipelineState.ENRICHED: (
            PipelineState.ASR_SUBMITTED,
            PipelineState.ASR_RUNNING,
            PipelineState.TRANSCRIBED,
            PipelineState.ENRICHED,
        ),
    }[target]
    for state in path:
        record = record.transition(state)
    transcript_result = (
        TranscriptResult(
            provider="siliconflow",
            provider_task_id="task",
            model="model",
            duration_ms=1,
            text="text",
        )
        if target
        in {
            PipelineState.TRANSCRIBED,
            PipelineState.ENRICHED,
        }
        else None
    )
    summary = (
        SummaryResult(
            summary="summary",
            mindmap=MindmapNode(node_id="root", title="root"),
            prompt_version="v1",
            model="model",
        )
        if target is PipelineState.ENRICHED
        else None
    )
    return EpisodeAIState(record=record, transcript=transcript_result, summary=summary)


class Store:
    def __init__(self, states: Mapping[str, EpisodeAIState]) -> None:
        self.states = states

    def load(self, _page: Mapping[str, object], eid: str) -> EpisodeAIState:
        return self.states[eid]


def final_state(eid: str, *, transcript: bool = False) -> EpisodeAIState:
    record = PipelineRecord(eid=eid, attempts=3).transition(
        PipelineState.FAILED_FINAL,
        failure=ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.SCHEMA_CHANGED,
            message="invalid",
        ),
    )
    transcript_result = (
        TranscriptResult(
            provider="siliconflow",
            provider_task_id="task",
            model="model",
            duration_ms=1,
            text="text",
        )
        if transcript
        else None
    )
    return EpisodeAIState(record=record, transcript=transcript_result)


def retryable_state(eid: str, *, resume_state: PipelineState) -> EpisodeAIState:
    record = PipelineRecord(eid=eid)
    if resume_state is not PipelineState.DISCOVERED:
        record = record.transition(resume_state)
    record = record.transition(
        PipelineState.FAILED_RETRYABLE,
        failure=ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.UNAVAILABLE,
            message="retryable",
        ),
    )
    return EpisodeAIState(record=record)


def test_manual_retry_selection_is_checked_and_category_ordered() -> None:
    pages = [
        episode("heard", played_seconds=120),
        episode("liked", liked=True),
        episode("favorite", favorite=True),
    ]
    states = {page["id"]: final_state(str(page["id"])) for page in pages}
    selected = select_manual_retry_work(pages, Store(states))
    assert [item.candidate.page_id for item in selected] == ["favorite", "liked", "heard"]
    assert all(manual_retry_requested(item.page) for item in selected)


def test_manual_retry_selection_includes_normal_checkpoints_and_unplayed_rows() -> None:
    pages = [
        episode("to-listen"),
        episode("heard", played_seconds=120),
        episode("liked", liked=True),
        episode("favorite", favorite=True),
        episode("published"),
    ]
    states = {
        "to-listen": normal_state("to-listen", PipelineState.DISCOVERED),
        "heard": normal_state("heard", PipelineState.ENRICHED),
        "liked": normal_state("liked", PipelineState.TRANSCRIBED),
        "favorite": normal_state("favorite", PipelineState.ASR_RUNNING),
        "published": EpisodeAIState(
            record=PipelineRecord(eid="published")
            .transition(PipelineState.ASR_SUBMITTED)
            .transition(PipelineState.ASR_RUNNING)
            .transition(PipelineState.TRANSCRIBED)
            .transition(PipelineState.ENRICHED)
            .transition(PipelineState.PUBLISHED),
        ),
    }
    selected = select_manual_retry_work(pages, Store(states))
    assert [item.candidate.page_id for item in selected] == [
        "favorite",
        "liked",
        "heard",
        "to-listen",
        "published",
    ]
    assert [item.state.record.state for item in selected] == [
        PipelineState.ASR_RUNNING,
        PipelineState.TRANSCRIBED,
        PipelineState.ENRICHED,
        PipelineState.DISCOVERED,
        PipelineState.PUBLISHED,
    ]


def test_manual_retry_target_uses_existing_checkpoints() -> None:
    assert manual_reopen_target(final_state("asr")) is PipelineState.DISCOVERED
    assert (
        manual_reopen_target(final_state("summary", transcript=True)) is PipelineState.TRANSCRIBED
    )
    enriched = final_state("enriched").model_copy(
        update={
            "summary": SummaryResult(
                summary="summary",
                mindmap=MindmapNode(node_id="root", title="root"),
                prompt_version="v1",
                model="model",
            )
        }
    )
    assert manual_reopen_target(enriched) is PipelineState.ENRICHED


def test_manual_retry_helpers_are_safe_for_malformed_pages_and_limits() -> None:
    assert manual_retry_requested({}) is False
    assert manual_retry_requested({"properties": []}) is False
    assert manual_retry_requested({"properties": {"人工请求重试": []}}) is False
    assert manual_retry_requested({"properties": {"人工请求重试": {"checkbox": False}}}) is False
    assert manual_reopen_target(EpisodeAIState(record=PipelineRecord(eid="ok"))) is None
    assert _limit(None) == MANUAL_RETRY_LIMIT
    assert _limit(99) == MANUAL_RETRY_LIMIT
    with pytest.raises(ValueError, match="positive"):
        _limit(0)
    assert "none=0" in ManualRetryQueueResult(0, 0, {}, {}).summary()


def test_manual_retry_selection_skips_unsafe_or_incomplete_rows() -> None:
    valid_final = episode("valid", played_seconds=120)
    unchecked = episode("unchecked")
    unchecked["properties"]["人工请求重试"] = {"checkbox": False}  # type: ignore[index]
    no_id = episode("no-id")
    no_id.pop("id")
    no_candidate = episode("no-candidate", played_seconds=0)
    no_candidate["properties"]["Skip AI"] = {"checkbox": True}  # type: ignore[index]
    retry_without_resume = episode("retry-without-resume", played_seconds=120)
    retry_without_resume["properties"]["ASR Status"] = {  # type: ignore[index]
        "select": {"name": "可重试失败"}
    }
    final_without_failure = episode("final-without-failure", played_seconds=120)
    published = episode("published", played_seconds=120)
    published["properties"]["Skip AI"] = {"checkbox": True}  # type: ignore[index]

    class StoreWithGaps(Store):
        def load(self, page: Mapping[str, object], eid: str) -> EpisodeAIState:
            if eid == "valid":
                raise NotionAPIError("broken state file")
            if eid == "retry-without-resume":
                return SimpleNamespace(
                    record=SimpleNamespace(
                        state=PipelineState.FAILED_RETRYABLE,
                        resume_state=None,
                        failure=ProviderFailure(
                            provider="x",
                            category=ProviderErrorCategory.UNAVAILABLE,
                            message="retry",
                        ),
                    )
                )
            if eid == "final-without-failure":
                return SimpleNamespace(
                    record=SimpleNamespace(
                        state=PipelineState.FAILED_FINAL,
                        resume_state=None,
                        failure=None,
                    )
                )
            if eid == "published":
                return SimpleNamespace(
                    record=SimpleNamespace(
                        state=PipelineState.PUBLISHED,
                        resume_state=None,
                        failure=None,
                    )
                )
            return super().load(page, eid)

    pages = [
        unchecked,
        no_id,
        no_candidate,
        retry_without_resume,
        final_without_failure,
        published,
        valid_final,
    ]
    states = {
        "no-candidate": final_state("no-candidate"),
        "valid": final_state("valid"),
    }
    assert select_manual_retry_work(pages, StoreWithGaps(states)) == ()


def test_manual_retry_queue_rejects_disabled_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(summary=SummaryConfig(enabled=False))
    monkeypatch.setattr(queue_module, "load_config", lambda _path: config)
    with pytest.raises(ConfigurationError, match="summary.enabled"):
        queue_module.run_manual_retry_queue(config_path="config.yaml")


def test_manual_retry_queue_rejects_missing_target_and_narrowing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(
        queue_module,
        "load_runtime_credentials",
        lambda: RuntimeCredentials(
            xiaoyuzhou_device_id="unused",
            notion_token=SecretStr("notion"),
            notion_page_id=None,
        ),
    )
    with pytest.raises(MissingCredentialError, match="Missing target page"):
        queue_module.run_manual_retry_queue(config_path="config.yaml")

    class UnnarrowedCredentials:
        notion_token = None
        notion_page_id = "root-page"

        def require(self, _name: str) -> None:
            return None

    monkeypatch.setattr(queue_module, "load_runtime_credentials", UnnarrowedCredentials)
    with pytest.raises(AssertionError, match="did not narrow"):
        queue_module.run_manual_retry_queue(config_path="config.yaml")


@dataclass
class _FakeContext:
    value: object | None = None

    def __enter__(self) -> object:
        return self if self.value is None else self.value

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeNotion(_FakeContext):
    def __init__(self, pages: list[dict[str, object]]) -> None:
        super().__init__()
        self.pages = pages
        self.retrieved: list[str] = []

    def query_data_source(
        self,
        _data_source_id: str,
        _payload: Mapping[str, object],
    ) -> list[dict[str, object]]:
        return self.pages

    def retrieve_page(self, page_id: str) -> dict[str, object]:
        self.retrieved.append(page_id)
        return next(page for page in self.pages if page.get("id") == page_id)


class _FakeStateContext(_FakeContext):
    def __init__(self, states: Mapping[str, EpisodeAIState]) -> None:
        super().__init__()
        self.states = dict(states)
        self.saved: list[str] = []
        self.cleared: list[str] = []

    def load(self, _page: Mapping[str, object], eid: str) -> EpisodeAIState:
        return self.states[eid]

    def save(self, page_id: str, state: EpisodeAIState) -> EpisodeAIState:
        self.states[state.record.eid] = state
        self.saved.append(page_id)
        return state

    def clear_manual_retry(self, page_id: str) -> None:
        self.cleared.append(page_id)


def test_run_manual_retry_queue_is_manual_first_and_stage_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        episode("retry-liked", liked=True),
        episode("final-favorite", favorite=True),
    ]
    states = {
        "retry-liked": retryable_state("retry-liked", resume_state=PipelineState.TRANSCRIBED),
        "final-favorite": final_state("final-favorite", transcript=True),
    }
    notion = _FakeNotion(pages)
    state_context = _FakeStateContext(states)
    provider = _FakeContext("provider")
    calls: list[tuple[str, bool]] = []
    sleeps: list[float] = []
    built_kwargs: dict[str, object] = {}

    class Initializer:
        def __init__(self, _api: object, page_id: str) -> None:
            assert page_id == "root-page"

        def initialize(self) -> object:
            return SimpleNamespace(
                resources={
                    "episode": SimpleNamespace(data_source_id="episode-ds"),
                    "mindmap": SimpleNamespace(data_source_id="mindmap-ds"),
                }
            )

    class Processor:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            built_kwargs.update(kwargs)

        def process(
            self,
            candidate: EpisodeCandidate,
            _page: object,
            *,
            retry_failed: bool = False,
        ) -> ProcessingOutcome:
            calls.append((candidate.page_id, retry_failed))
            if candidate.page_id == "retry-liked":
                return ProcessingOutcome(candidate.eid, "pending", PipelineState.ASR_RUNNING)
            return ProcessingOutcome(candidate.eid, "published", PipelineState.PUBLISHED)

    monkeypatch.setattr(queue_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(
        queue_module,
        "load_runtime_credentials",
        lambda: RuntimeCredentials(
            xiaoyuzhou_device_id="unused",
            notion_token=SecretStr("notion"),
            notion_page_id="root-page",
            dashscope_api_key=SecretStr("dashscope"),
            siliconflow_api_key=SecretStr("siliconflow"),
        ),
    )
    monkeypatch.setattr(queue_module, "NotionClient", lambda _token: notion)
    monkeypatch.setattr(queue_module, "NotionInitializer", Initializer)
    monkeypatch.setattr(queue_module, "NotionEpisodeStateStore", lambda _api: state_context)
    monkeypatch.setattr(
        queue_module,
        "build_provider_clients",
        lambda **kwargs: (
            built_kwargs.update(provider_kwargs=kwargs) or (provider, provider, None, provider)
        ),
    )
    monkeypatch.setattr(queue_module, "EpisodeAIProcessor", Processor)
    monkeypatch.setattr(queue_module.time, "sleep", sleeps.append)
    preflights: list[object] = []
    monkeypatch.setattr(
        queue_module,
        "preflight_summary_client",
        lambda client: preflights.append(client),
    )

    progress: list[str] = []
    result = queue_module.run_manual_retry_queue(
        config_path="config.yaml",
        requested_limit=2,
        progress=progress.append,
    )

    assert calls == [("final-favorite", False), ("retry-liked", True)]
    assert notion.retrieved == ["final-favorite"]
    assert state_context.saved == ["final-favorite"]
    assert state_context.cleared == ["final-favorite"]
    assert sleeps == [MANUAL_RETRY_INTER_EPISODE_SECONDS]
    assert built_kwargs["provider_kwargs"]
    assert result.selected == 2
    assert result.remaining == 0
    assert result.actions == {"pending": 1, "published": 1}
    assert result.states == {"ASR_RUNNING": 1, "PUBLISHED": 1}
    assert result.categories == {"favorite": 1, "liked": 1}
    assert preflights == [provider]
    assert progress == [
        "Manual retry queue selection (selected=2; available=2)",
        "Manual retry summary preflight started",
        "Manual retry summary preflight OK",
        "Manual retry item 1/2 started (checkpoint=FAILED_FINAL)",
        "Manual retry item 1/2 finished (action=published; state=PUBLISHED)",
        "Manual retry item 2/2 started (checkpoint=FAILED_RETRYABLE)",
        "Manual retry item 2/2 finished (action=pending; state=ASR_RUNNING)",
    ]
