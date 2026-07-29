from collections.abc import Mapping, Sequence
from typing import Any

from xyz2notion.models import MindmapNode, SummaryResult
from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.mindmap_database import (
    MindmapDatabaseSynchronizer,
    render_mindmap_mermaid,
)


class FakeRows:
    def __init__(self) -> None:
        self.pages: list[JsonObject] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def query_data_source(
        self,
        _data_source_id: str,
        _payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        return list(self.pages)

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
        self.pages.append(page)
        return page

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.updates.append((page_id, dict(payload)))
        return {"id": page_id}


def summary() -> SummaryResult:
    return SummaryResult(
        summary="摘要",
        mindmap=MindmapNode(
            node_id="root",
            title="主题",
            children=(MindmapNode(node_id="child", title="分支(A)"),),
        ),
        prompt_version="v1",
        model="model",
    )


def test_mindmap_database_sync_creates_relation_and_stable_key() -> None:
    api = FakeRows()
    result = MindmapDatabaseSynchronizer(api, "mindmaps").sync(
        eid="episode-1",
        episode_page_id="episode-page",
        episode_title="单集",
        summary=summary(),
        content_version="hash",
    )
    assert result.action == "created"
    properties = api.pages[0]["properties"]
    assert properties["Episode"] == {"relation": [{"id": "episode-page"}]}
    assert properties["Mindmap Key"]["rich_text"][0]["text"]["content"] == "episode-1"
    assert properties["Content Version"]["rich_text"][0]["text"]["content"] == "hash"


def test_mermaid_render_is_deterministic_and_sanitized() -> None:
    rendered = render_mindmap_mermaid(summary().mindmap)
    assert rendered == 'mindmap\n  "主题"\n    "分支[A]"'


def test_mindmap_database_sync_is_unchanged_for_identical_existing_row() -> None:
    api = FakeRows()
    synchronizer = MindmapDatabaseSynchronizer(api, "mindmaps")
    persisted_summary = summary()
    first = synchronizer.sync(
        eid="episode-1",
        episode_page_id="episode-page",
        episode_title="单集",
        summary=persisted_summary,
        content_version="hash",
    )
    second = synchronizer.sync(
        eid="episode-1",
        episode_page_id="episode-page",
        episode_title="单集",
        summary=persisted_summary,
        content_version="hash",
    )
    assert first.action == "created"
    assert second.action == "unchanged"
    assert api.updates == []
