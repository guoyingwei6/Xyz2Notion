import json

import httpx
import pytest

from xyz2notion.models import (
    MindmapNode,
    SummaryResult,
    TranscriptResult,
)
from xyz2notion.notion.client import NotionAPIError
from xyz2notion.orchestration.state_store import (
    EpisodeAIState,
    NotionEpisodeStateStore,
)
from xyz2notion.state import PipelineRecord, PipelineState


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
    )
    summary = SummaryResult(
        summary="摘要",
        mindmap=MindmapNode(node_id="root", title="主题"),
        prompt_version="summary-v1",
        model="Qwen/Qwen3-8B",
    )
    record = PipelineRecord(eid="episode").transition(PipelineState.TRANSCRIBED)
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
    assert payload["properties"]["转写完成时间"]["date"]["start"] == (
        state.transcript.created_at.isoformat()  # type: ignore[union-attr]
    )
    assert payload["properties"]["总结完成时间"]["date"]["start"] == (
        state.summary.created_at.isoformat()  # type: ignore[union-attr]
    )
    assert payload["properties"]["AI State File"]["files"][0]["file_upload"]["id"] == ("upload-1")


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
