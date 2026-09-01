import json

import httpx
import pytest

from xyz2notion.models import (
    MindmapNode,
    ProviderErrorCategory,
    ProviderFailure,
    SummaryResult,
    TranscriptResult,
)
from xyz2notion.notion.client import NotionAPIError
from xyz2notion.orchestration.state_store import (
    EpisodeAIState,
    NotionEpisodeStateStore,
    _asr_status,
    _enrichment_provider,
    _enrichment_status,
    _file_url,
    _summary_provider_name,
)
from xyz2notion.state import InvalidStateTransitionError, PipelineRecord, PipelineState


class FakeAPI:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []
        self.updates: list[tuple[str, object]] = []

    def upload_file(self, filename: str, content_type: str, content: bytes) -> str:
        self.uploads.append((filename, content_type, content))
        return "upload-1"

    def update_page(self, page_id: str, payload: object) -> dict[str, object]:
        self.updates.append((page_id, payload))
        return {"id": page_id}


def full_state() -> EpisodeAIState:
    transcript = TranscriptResult(
        provider="siliconflow",
        provider_task_id="task",
        model="model",
        duration_ms=1,
        text="文字稿",
        accuracy_hint=0.9,
    )
    summary = SummaryResult(
        summary="摘要",
        mindmap=MindmapNode(node_id="root", title="主题"),
        prompt_version="summary-v1",
        model="Qwen/Qwen3-8B",
        provider="siliconflow_summary",
    )
    record = (
        PipelineRecord(eid="episode")
        .transition(PipelineState.TRANSCRIBED)
        .transition(PipelineState.ENRICHED)
    )
    return EpisodeAIState(
        record=record,
        provider="siliconflow",
        provider_task_id="task",
        transcript=transcript,
        summary=summary,
        content_version="version",
    )


def page_with_state(url: str) -> dict[str, object]:
    return {
        "properties": {
            "AI State File": {
                "files": [{"file": {"url": url}}],
            }
        }
    }


def test_enrichment_metadata_tracks_the_independent_pipeline() -> None:
    assert _summary_provider_name(None) == ""
    assert _enrichment_status(PipelineRecord(eid="discovered")) == "未开始"
    transcribed = PipelineRecord(eid="transcribed").transition(PipelineState.TRANSCRIBED)
    assert _enrichment_status(transcribed) == "待增强"
    enriched = transcribed.transition(PipelineState.ENRICHED)
    assert _enrichment_status(enriched) == "待发布"
    assert _enrichment_status(enriched.transition(PipelineState.PUBLISHED)) == "已完成"

    asr_retry = (
        PipelineRecord(eid="asr-retry")
        .transition(PipelineState.ASR_SUBMITTED)
        .transition(PipelineState.ASR_RUNNING)
        .transition(
            PipelineState.FAILED_RETRYABLE,
            failure=ProviderFailure(
                provider="dashscope",
                category=ProviderErrorCategory.TIMEOUT,
                message="retry",
            ),
        )
    )
    assert _enrichment_status(asr_retry) == "未开始"

    summary_retry = transcribed.transition(
        PipelineState.FAILED_RETRYABLE,
        failure=ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.TIMEOUT,
            message="retry",
        ),
    )
    assert _enrichment_status(summary_retry) == "可重试失败"
    summary_final = transcribed.transition(
        PipelineState.FAILED_FINAL,
        failure=ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.SCHEMA_CHANGED,
            message="invalid",
        ),
    )
    assert _enrichment_status(summary_final) == "最终失败"
    assert _enrichment_provider(EpisodeAIState(record=summary_final)) == ("siliconflow_summary")
    dashscope_final = summary_final.model_copy(
        update={
            "failure": summary_final.failure.model_copy(  # type: ignore[union-attr]
                update={"provider": "dashscope_summary"}
            )
        }
    )
    assert _enrichment_status(dashscope_final) == "最终失败"
    assert _enrichment_provider(EpisodeAIState(record=dashscope_final)) == "dashscope_summary"
    assert _enrichment_provider(EpisodeAIState(record=asr_retry)) == ""
    old_local = SummaryResult(
        summary="摘要",
        mindmap=MindmapNode(node_id="root", title="主题"),
        prompt_version="v1",
        model="local/Qwen3-1.7B-Q4_K_M",
    )
    assert _enrichment_provider(EpisodeAIState(record=enriched, summary=old_local)) == (
        "local_qwen_summary"
    )


def test_state_file_url_parser_rejects_malformed_properties_and_accepts_external() -> None:
    assert _file_url({}) is None
    assert _file_url({"properties": {"AI State File": {}}}) is None
    assert _file_url({"properties": {"AI State File": {"files": []}}}) is None
    assert _file_url({"properties": {"AI State File": {"files": [{}]}}}) is None
    assert _file_url({"properties": {"AI State File": {"files": [{"file": {}}]}}}) is None
    assert (
        _file_url(
            {
                "properties": {
                    "AI State File": {
                        "files": [{"external": {"url": "https://files.example/state.json"}}]
                    }
                }
            }
        )
        == "https://files.example/state.json"
    )
    assert _file_url({"properties": {"AI State File": {"files": ["not-a-file"]}}}) is None


def test_state_store_context_manager_closes_owned_client() -> None:
    with NotionEpisodeStateStore(FakeAPI()):
        pass


def test_state_store_rejects_missing_redirect_location() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(302)))
    with pytest.raises(NotionAPIError, match="no Location"):
        NotionEpisodeStateStore(FakeAPI(), http_client=http).load(
            page_with_state("https://files.example/state.json"),
            "episode",
        )


def test_state_store_rejects_oversized_response_and_redirect_loop() -> None:
    oversized = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * (20 * 1024 * 1024 + 1))
        )
    )
    with pytest.raises(NotionAPIError, match="exceeds 20 MiB"):
        NotionEpisodeStateStore(FakeAPI(), http_client=oversized).load(
            page_with_state("https://files.example/state.json"),
            "episode",
        )

    loop = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                302,
                headers={"Location": "https://files.example/state.json"},
            )
        )
    )
    with pytest.raises(NotionAPIError, match="redirect limit"):
        NotionEpisodeStateStore(FakeAPI(), http_client=loop).load(
            page_with_state("https://files.example/state.json"),
            "episode",
        )


def test_state_store_save_handles_no_transcript_and_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeAPI()
    store = NotionEpisodeStateStore(
        api,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: None)),
    )
    saved = store.save("page", EpisodeAIState(record=PipelineRecord(eid="empty")))
    assert saved.record.state is PipelineState.DISCOVERED
    assert api.updates[-1][1]["properties"]["增强状态"]["select"]["name"] == "未开始"
    monkeypatch.setattr("xyz2notion.orchestration.state_store.MAX_STATE_BYTES", 1)
    with pytest.raises(NotionAPIError, match="exceeds 20 MiB"):
        store.save("page", EpisodeAIState(record=PipelineRecord(eid="too-large")))


def test_missing_state_file_initializes_discovered_record() -> None:
    store = NotionEpisodeStateStore(
        FakeAPI(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: None)),
    )
    state = store.load({"properties": {}}, "episode")
    assert state.record.state is PipelineState.DISCOVERED
    assert state.record.eid == "episode"


def test_save_uploads_private_json_then_switches_episode_property() -> None:
    api = FakeAPI()
    store = NotionEpisodeStateStore(
        api,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: None)),
    )
    state = full_state()
    saved = store.save("page", state)
    assert saved.state_revision == 1
    assert api.uploads[0][0].endswith("-r0001.json")
    assert api.uploads[0][1] == "application/json"
    decoded = EpisodeAIState.model_validate_json(api.uploads[0][2])
    assert decoded.transcript is not None
    assert decoded.transcript.text == "文字稿"

    page_id, raw_payload = api.updates[0]
    payload = raw_payload  # type: ignore[assignment]
    assert page_id == "page"
    assert payload["properties"]["ASR Status"]["select"]["name"] == "已转写"
    assert payload["properties"]["ASR Provider"]["rich_text"][0]["text"]["content"] == (
        "siliconflow"
    )
    assert payload["properties"]["增强 Provider"]["rich_text"][0]["text"]["content"] == (
        "siliconflow_summary"
    )
    assert payload["properties"]["增强状态"]["select"]["name"] == "待发布"
    assert payload["properties"]["转写完成时间"]["date"]["start"] == (
        state.transcript.created_at.isoformat()  # type: ignore[union-attr]
    )
    assert payload["properties"]["总结完成时间"]["date"]["start"] == (
        state.summary.created_at.isoformat()  # type: ignore[union-attr]
    )
    assert payload["properties"]["AI State File"]["files"][0]["file_upload"]["id"] == ("upload-1")


def test_asr_status_stays_transcribed_when_summary_fails() -> None:
    transcript = full_state().transcript
    assert transcript is not None
    record = (
        PipelineRecord(eid="episode")
        .transition(PipelineState.TRANSCRIBED)
        .transition(
            PipelineState.FAILED_FINAL,
            failure=ProviderFailure(
                provider="summary_fallback_chain",
                category=ProviderErrorCategory.SCHEMA_CHANGED,
                message="safe failure",
            ),
        )
    )
    state = EpisodeAIState(record=record, transcript=transcript)
    assert _asr_status(state) == "已转写"
    assert _enrichment_status(record) == "最终失败"


def test_clear_manual_retry_only_unchecks_the_request() -> None:
    api = FakeAPI()
    store = NotionEpisodeStateStore(
        api,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: None)),
    )
    store.clear_manual_retry("page")
    assert api.updates == [("page", {"properties": {"人工请求重试": {"checkbox": False}}})]


def test_manual_reopen_resets_final_failure_to_requested_checkpoint() -> None:
    final = PipelineRecord(
        eid="episode",
        attempts=3,
    ).transition(
        PipelineState.FAILED_FINAL,
        failure=ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.SCHEMA_CHANGED,
            message="invalid",
        ),
    )
    reopened = final.manual_reopen(PipelineState.TRANSCRIBED)
    assert reopened.state is PipelineState.TRANSCRIBED
    assert reopened.attempts == 0
    assert reopened.failure is None
    assert reopened.resume_state is None
    assert reopened.history[-1].to_state is PipelineState.TRANSCRIBED
    with pytest.raises(InvalidStateTransitionError, match="FAILED_FINAL"):
        reopened.manual_reopen(PipelineState.DISCOVERED)


def test_load_follows_safe_redirect_and_validates_episode() -> None:
    content = full_state().model_dump_json().encode()
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "files.example":
            return httpx.Response(
                302,
                headers={"Location": "https://download.example/state.json"},
            )
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        store = NotionEpisodeStateStore(FakeAPI(), http_client=http)
        loaded = store.load(
            page_with_state("https://files.example/state.json"),
            "episode",
        )
    assert loaded.summary is not None
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(503), "HTTP 503"),
        (httpx.Response(200, content=b"not-json"), "invalid"),
    ],
)
def test_invalid_state_download_is_safe(
    response: httpx.Response,
    message: str,
) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http:
        store = NotionEpisodeStateStore(FakeAPI(), http_client=http)
        with pytest.raises(NotionAPIError, match=message):
            store.load(page_with_state("https://files.example/state.json"), "episode")

    wrong = full_state().model_dump(mode="json")
    wrong["record"]["eid"] = "other"
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=json.dumps(wrong).encode())
            )
        ) as http,
        pytest.raises(NotionAPIError, match="different Episode"),
    ):
        NotionEpisodeStateStore(FakeAPI(), http_client=http).load(
            page_with_state("https://files.example/state.json"),
            "episode",
        )


def test_external_file_shape_and_context_manager_are_supported() -> None:
    content = EpisodeAIState(record=PipelineRecord(eid="episode")).model_dump_json()
    page = {
        "properties": {
            "AI State File": {"files": [{"external": {"url": "https://files.example/state.json"}}]}
        }
    }
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=content.encode()))
    with (
        httpx.Client(transport=transport) as http,
        NotionEpisodeStateStore(FakeAPI(), http_client=http) as store,
    ):
        assert store.load(page, "episode").record.eid == "episode"
