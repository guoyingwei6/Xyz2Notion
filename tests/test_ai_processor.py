from collections.abc import Mapping, Sequence
from typing import Any

from xyz2notion.asr.tingwu import (
    TingwuEnrichment,
    TingwuTask,
    TingwuTaskState,
)
from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.enrichment.siliconflow import CompletionUsage
from xyz2notion.models import (
    MindmapNode,
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    SummaryResult,
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)
from xyz2notion.notion.client import JsonObject
from xyz2notion.orchestration.processor import (
    EpisodeAIProcessor,
    EpisodeCandidate,
    episode_candidates,
)
from xyz2notion.orchestration.state_store import EpisodeAIState
from xyz2notion.state import PipelineRecord, PipelineState


def transcript(provider: str = "siliconflow") -> TranscriptResult:
    return TranscriptResult(
        provider=provider,
        provider_task_id="task",
        model="model",
        duration_ms=1_000,
        text="文字稿",
        segments=(TranscriptSegment(start_ms=0, end_ms=1_000, text="文字稿"),),
        timing_quality=TranscriptTimingQuality.EXACT,
    )


class FakeStateStore:
    def __init__(self, initial: EpisodeAIState | None = None) -> None:
        self.state = initial or EpisodeAIState(record=PipelineRecord(eid="episode"))
        self.saved: list[EpisodeAIState] = []

    def load(self, _page: Mapping[str, Any], _eid: str) -> EpisodeAIState:
        return self.state

    def save(self, _page_id: str, state: EpisodeAIState) -> EpisodeAIState:
        state = state.model_copy(update={"state_revision": state.state_revision + 1})
        self.state = state
        self.saved.append(state)
        return state


class FakeNotion:
    def __init__(self) -> None:
        self.children: list[JsonObject] = []
        self.blocks: dict[str, JsonObject] = {}
        self.next_id = 1

    def list_block_children(self, _block_id: str) -> list[JsonObject]:
        return list(self.children)

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]:
        results = []
        for child in children:
            identifier = f"block-{self.next_id}"
            self.next_id += 1
            block = {**dict(child), "id": identifier}
            self.blocks[identifier] = block
            results.append(block)
            if block_id == "page":
                self.children.append(block)
        return results

    def update_block(self, block_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.blocks[block_id].update(payload)
        return self.blocks[block_id]

    def delete_block(self, block_id: str) -> JsonObject:
        self.children = [block for block in self.children if block.get("id") != block_id]
        return {"id": block_id}

    def upload_file(self, _filename: str, _content_type: str, _content: bytes) -> str:
        return "upload"


class FailingPublishNotion(FakeNotion):
    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]:
        if block_id.startswith("block-"):
            raise RuntimeError("fixture detail must not be persisted")
        return super().append_block_children(block_id, children)


class FakeMindmapNotion(FakeNotion):
    def __init__(self) -> None:
        super().__init__()
        self.mindmap_pages: list[JsonObject] = []

    def query_data_source(
        self,
        _data_source_id: str,
        _payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        return list(self.mindmap_pages)

    def create_data_source_page(
        self,
        _data_source_id: str,
        properties: Mapping[str, Any],
        *,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
        children: Sequence[Mapping[str, Any]] = (),
    ) -> JsonObject:
        page = {"id": "mindmap-page", "properties": dict(properties)}
        self.mindmap_pages.append(page)
        return page

    def update_page(self, page_id: str, _payload: Mapping[str, Any]) -> JsonObject:
        return {"id": page_id}


class FakeSummaryClient:
    models = ("Qwen/Qwen3-8B",)
    active_model = "Qwen/Qwen3-8B"

    def generate_structured(
        self,
        _model_type: object,
        **_kwargs: object,
    ) -> tuple[EnrichmentPayload, CompletionUsage]:
        return (
            EnrichmentPayload(
                summary="摘要",
                mindmap=MindmapNode(node_id="root", title="主题"),
            ),
            CompletionUsage(10, 5),
        )


class SiliconProcessor(EpisodeAIProcessor):
    def __init__(self, *args: object, failures: int = 0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.failures = failures
        self.asr_calls = 0

    def _siliconflow(self, _candidate: EpisodeCandidate) -> TranscriptResult:
        self.asr_calls += 1
        if self.asr_calls <= self.failures:
            raise ProviderError(
                ProviderFailure(
                    provider="siliconflow",
                    category=ProviderErrorCategory.RATE_LIMITED,
                    message="retry",
                )
            )
        return transcript()


class LocalFallbackProcessor(SiliconProcessor):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.local_calls = 0

    def _local_whisper(self, _candidate: EpisodeCandidate) -> TranscriptResult:
        self.local_calls += 1
        return transcript("local_whisper")


class FakeTingwu:
    def __init__(self, task: TingwuTask, *, fail_auth: bool = False) -> None:
        self.task = task
        self.fail_auth = fail_auth
        self.calls = 0

    def submit_episode(self, *_args: object, **_kwargs: object) -> TingwuTask:
        self.calls += 1
        if self.fail_auth:
            raise ProviderError(
                ProviderFailure(
                    provider="tingwu_cookie",
                    category=ProviderErrorCategory.AUTHENTICATION,
                    message="expired",
                )
            )
        return self.task

    def get_transcript(self, _task_id: str) -> TranscriptResult:
        return transcript("tingwu_cookie")

    def get_enrichment(self, _task_id: str) -> TingwuEnrichment:
        return TingwuEnrichment(
            summary="听悟摘要",
            mindmap={"content": "主题", "children": []},
        )


CANDIDATE = EpisodeCandidate("page", "episode", "标题", "https://cdn.example/audio")


def test_candidate_extraction_skips_incomplete_rows() -> None:
    pages = [
        {
            "id": "page",
            "properties": {
                "EID": {"rich_text": [{"plain_text": "episode"}]},
                "Name": {"title": [{"text": {"content": "标题"}}]},
                "Audio URL": {"url": "https://cdn.example/audio"},
                "Played Seconds": {"number": 120},
            },
        },
        {
            "id": "legacy-published",
            "properties": {
                "EID": {"rich_text": [{"plain_text": "legacy"}]},
                "Name": {"title": [{"plain_text": "旧单集"}]},
                "Audio URL": {"url": "https://cdn.example/legacy"},
                "Played Seconds": {"number": 120},
                "ASR Status": {"select": {"name": "已发布"}},
            },
        },
        {
            "id": "too-short",
            "properties": {
                "EID": {"rich_text": [{"plain_text": "short"}]},
                "Name": {"title": [{"plain_text": "太短"}]},
                "Audio URL": {"url": "https://cdn.example/short"},
                "Played Seconds": {"number": 119},
            },
        },
        {
            "id": "opted-out",
            "properties": {
                "EID": {"rich_text": [{"plain_text": "opted-out"}]},
                "Name": {"title": [{"plain_text": "不转写"}]},
                "Audio URL": {"url": "https://cdn.example/opted-out"},
                "Played Seconds": {"number": 600},
                "Skip AI": {"checkbox": True},
            },
        },
        {
            "id": "favorited",
            "properties": {
                "EID": {"rich_text": [{"plain_text": "favorite"}]},
                "Name": {"title": [{"plain_text": "收藏单集"}]},
                "Audio URL": {"url": "https://cdn.example/favorite"},
                "Played Seconds": {"number": 0},
                "Favorited": {"checkbox": True},
            },
        },
        {"id": "skip", "properties": {}},
        {"properties": {}},
    ]
    assert episode_candidates(pages) == (
        CANDIDATE,
        EpisodeCandidate(
            "favorited",
            "favorite",
            "收藏单集",
            "https://cdn.example/favorite",
        ),
    )


def test_siliconflow_summary_and_publish_are_checkpointed() -> None:
    store = FakeStateStore()
    processor = SiliconProcessor(
        FakeNotion(),
        store,
        siliconflow=object(),
        summary_client=FakeSummaryClient(),
    )
    outcome = processor.process(CANDIDATE, {})
    assert outcome.state is PipelineState.PUBLISHED
    assert outcome.action == "created"
    assert processor.asr_calls == 1
    assert [state.record.state for state in store.saved] == [
        PipelineState.TRANSCRIBED,
        PipelineState.ENRICHED,
        PipelineState.PUBLISHED,
    ]


def test_publish_also_upserts_standalone_mindmap_database() -> None:
    notion = FakeMindmapNotion()
    outcome = SiliconProcessor(
        notion,
        FakeStateStore(),
        siliconflow=object(),
        summary_client=FakeSummaryClient(),
        mindmap_data_source_id="mindmaps",
    ).process(CANDIDATE, {})
    assert outcome.state is PipelineState.PUBLISHED
    assert len(notion.mindmap_pages) == 1
    properties = notion.mindmap_pages[0]["properties"]
    assert properties["Episode"] == {"relation": [{"id": "page"}]}
    assert properties["Mindmap Key"]["rich_text"][0]["text"]["content"] == "episode"


def test_no_asr_provider_pauses_without_persisting_failure() -> None:
    store = FakeStateStore()
    outcome = EpisodeAIProcessor(FakeNotion(), store).process(CANDIDATE, {})
    assert outcome.action == "paused"
    assert outcome.state is PipelineState.DISCOVERED
    assert store.saved == []


def test_siliconflow_failure_falls_back_to_local_whisper() -> None:
    store = FakeStateStore()
    processor = LocalFallbackProcessor(
        FakeNotion(),
        store,
        siliconflow=object(),
        local_whisper=object(),  # type: ignore[arg-type]
        summary_client=FakeSummaryClient(),
        failures=1,
    )
    outcome = processor.process(CANDIDATE, {})
    assert outcome.state is PipelineState.PUBLISHED
    assert processor.asr_calls == 1
    assert processor.local_calls == 1
    assert store.state.provider == "local_whisper"


def test_local_whisper_can_run_without_remote_asr_provider() -> None:
    store = FakeStateStore()
    processor = LocalFallbackProcessor(
        FakeNotion(),
        store,
        local_whisper=object(),  # type: ignore[arg-type]
        summary_client=FakeSummaryClient(),
    )
    outcome = processor.process(CANDIDATE, {})
    assert outcome.state is PipelineState.PUBLISHED
    assert processor.asr_calls == 0
    assert processor.local_calls == 1


def test_disabled_summary_pauses_after_transcription_without_repeating_asr() -> None:
    state = EpisodeAIState(
        record=PipelineRecord(eid="episode").transition(PipelineState.TRANSCRIBED),
        transcript=transcript(),
    )
    store = FakeStateStore(state)
    outcome = EpisodeAIProcessor(
        FakeNotion(),
        store,
        summary_enabled=False,
    ).process(CANDIDATE, {})
    assert outcome.action == "summary_paused"
    assert outcome.state is PipelineState.TRANSCRIBED
    assert store.saved == []


def test_transcribed_checkpoint_waits_for_summary_key_without_repeating_asr() -> None:
    record = PipelineRecord(eid="episode").transition(PipelineState.TRANSCRIBED)
    store = FakeStateStore(EpisodeAIState(record=record, transcript=transcript()))
    processor = SiliconProcessor(FakeNotion(), store, siliconflow=object())
    outcome = processor.process(CANDIDATE, {})
    assert outcome.action == "waiting_summary_key"
    assert processor.asr_calls == 0
    assert store.saved == []


def test_tingwu_processing_is_persisted_and_not_fallen_back() -> None:
    task = TingwuTask(
        provider_task_id="source",
        source_task_id="source",
        state=TingwuTaskState.SOURCE_PARSING,
        directory_id="dir",
        title="标题",
    )
    store = FakeStateStore()
    processor = EpisodeAIProcessor(
        FakeNotion(),
        store,  # type: ignore[arg-type]
        tingwu=FakeTingwu(task),  # type: ignore[arg-type]
        siliconflow=object(),  # type: ignore[arg-type]
    )
    outcome = processor.process(CANDIDATE, {})
    assert outcome.action == "pending"
    assert outcome.state is PipelineState.ASR_SUBMITTED
    assert store.state.source_task_id == "source"


def test_tingwu_native_success_publishes_without_summary_api() -> None:
    task = TingwuTask(
        provider_task_id="record",
        state=TingwuTaskState.SUCCEEDED,
        directory_id="dir",
        title="标题",
        record_status=30,
    )
    store = FakeStateStore()
    processor = EpisodeAIProcessor(
        FakeNotion(),
        store,  # type: ignore[arg-type]
        tingwu=FakeTingwu(task),  # type: ignore[arg-type]
    )
    outcome = processor.process(CANDIDATE, {})
    assert outcome.state is PipelineState.PUBLISHED
    assert store.state.summary is not None
    assert store.state.summary.prompt_version == "tingwu-native"


def test_expired_tingwu_falls_back_to_siliconflow() -> None:
    task = TingwuTask(
        provider_task_id="unused",
        state=TingwuTaskState.PROCESSING,
        directory_id="dir",
        title="标题",
    )
    store = FakeStateStore()
    processor = SiliconProcessor(
        FakeNotion(),
        store,
        tingwu=FakeTingwu(task, fail_auth=True),
        siliconflow=object(),
        summary_client=FakeSummaryClient(),
    )
    outcome = processor.process(CANDIDATE, {})
    assert outcome.state is PipelineState.PUBLISHED
    assert processor.asr_calls == 1
    assert store.state.provider == "siliconflow"


def test_retryable_failure_resumes_exact_stage_on_manual_retry() -> None:
    store = FakeStateStore()
    processor = SiliconProcessor(
        FakeNotion(),
        store,
        siliconflow=object(),
        summary_client=FakeSummaryClient(),
        failures=1,
    )
    failed = processor.process(CANDIDATE, {})
    assert failed.state is PipelineState.FAILED_RETRYABLE
    assert store.state.record.resume_state is PipelineState.DISCOVERED

    waiting = processor.process(CANDIDATE, {})
    assert waiting.action == "waiting_retry"
    completed = processor.process(CANDIDATE, {}, retry_failed=True)
    assert completed.state is PipelineState.PUBLISHED
    assert store.state.record.attempts == 1


def test_publish_failure_is_checkpointed_and_resumes_without_repeating_ai() -> None:
    record = PipelineRecord(eid="episode").transition(PipelineState.TRANSCRIBED)
    record = record.transition(PipelineState.ENRICHED)
    store = FakeStateStore(
        EpisodeAIState(
            record=record,
            transcript=transcript(),
            summary=SummaryResult(
                summary="摘要",
                mindmap=MindmapNode(node_id="root", title="主题"),
                prompt_version="summary-v1",
                model="Qwen/Qwen3-8B",
            ),
        )
    )
    failed = EpisodeAIProcessor(FailingPublishNotion(), store).process(CANDIDATE, {})
    assert failed.state is PipelineState.FAILED_RETRYABLE
    assert store.state.record.resume_state is PipelineState.ENRICHED
    assert store.state.record.failure is not None
    assert store.state.record.failure.provider == "notion_publish"
    assert "fixture detail" not in store.state.record.failure.message

    completed = EpisodeAIProcessor(FakeNotion(), store).process(
        CANDIDATE,
        {},
        retry_failed=True,
    )
    assert completed.state is PipelineState.PUBLISHED
    assert store.state.record.attempts == 1


def test_published_and_final_failed_rows_are_skipped() -> None:
    published_record = PipelineRecord(eid="episode")
    published_record = published_record.transition(PipelineState.TRANSCRIBED)
    published_record = published_record.transition(PipelineState.ENRICHED)
    published_record = published_record.transition(PipelineState.PUBLISHED)
    store = FakeStateStore(EpisodeAIState(record=published_record))
    assert SiliconProcessor(FakeNotion(), store).process(CANDIDATE, {}).action == "skipped"

    final_record = PipelineRecord(eid="episode").transition(
        PipelineState.FAILED_FINAL,
        failure=ProviderFailure(
            provider="fixture",
            category=ProviderErrorCategory.AUTHENTICATION,
            message="final",
        ),
    )
    final_store = FakeStateStore(EpisodeAIState(record=final_record))
    assert SiliconProcessor(FakeNotion(), final_store).process(CANDIDATE, {}).action == "skipped"
