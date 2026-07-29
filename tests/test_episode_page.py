from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from xyz2notion.models import (
    Chapter,
    MindmapNode,
    SummaryResult,
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)
from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.episode_page import (
    MANAGED_PREFIX,
    EpisodePageInput,
    EpisodePageRenderer,
)


def block_text(block: Mapping[str, Any]) -> str:
    block_type = block.get("type")
    body = block.get(block_type) if isinstance(block_type, str) else None
    if not isinstance(body, Mapping):
        return ""
    values = body.get("rich_text", [])
    return "".join(
        str(item.get("text", {}).get("content") or "")
        for item in values
        if isinstance(item, Mapping)
    )


class FakeNotion:
    def __init__(self, page_children: list[JsonObject] | None = None) -> None:
        self.page_children = list(page_children or [])
        self.blocks: dict[str, JsonObject] = {
            str(block["id"]): block for block in self.page_children if block.get("id")
        }
        self.appended: list[tuple[str, list[JsonObject]]] = []
        self.deleted: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.next_id = 1
        self.fail_after_root = False

    def list_block_children(self, block_id: str) -> list[JsonObject]:
        assert block_id == "episode-page"
        return list(self.page_children)

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]:
        values = [dict(child) for child in children]
        if self.fail_after_root and block_id.startswith("new-"):
            raise RuntimeError("injected append failure")
        self.appended.append((block_id, values))
        results = []
        for value in values:
            identifier = f"new-{self.next_id}"
            self.next_id += 1
            created = {**value, "id": identifier}
            self.blocks[identifier] = created
            results.append(created)
            if block_id == "episode-page":
                self.page_children.append(created)
        return results

    def update_block(
        self,
        block_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        self.blocks[block_id].update(payload)
        return self.blocks[block_id]

    def delete_block(self, block_id: str) -> JsonObject:
        self.deleted.append(block_id)
        self.page_children = [
            block for block in self.page_children if str(block.get("id")) != block_id
        ]
        return {"id": block_id, "archived": True}

    def upload_file(self, filename: str, content_type: str, content: bytes) -> str:
        self.uploads.append((filename, content_type, content))
        return f"upload-{len(self.uploads)}"


def transcript(text: str = "第一句") -> TranscriptResult:
    return TranscriptResult(
        provider="siliconflow",
        provider_task_id="task",
        model="SenseVoiceSmall",
        duration_ms=5_000,
        text=text,
        segments=(TranscriptSegment(start_ms=0, end_ms=5_000, text=text, speaker="主播"),),
        timing_quality=TranscriptTimingQuality.COARSE,
    )


def summary() -> SummaryResult:
    return SummaryResult(
        summary="全文摘要",
        chapters=(Chapter(start_ms=0, title="开场", summary="章节摘要"),),
        highlights=("观点",),
        quotes=("金句",),
        terms=("术语",),
        people=("人物",),
        questions=("问题",),
        mindmap=MindmapNode(
            node_id="root",
            title="主题",
            children=(
                MindmapNode(
                    node_id="child",
                    title="子观点",
                    children=(MindmapNode(node_id="deep", title="细节"),),
                ),
            ),
        ),
        prompt_version="summary-v1",
        model="Qwen/Qwen3-8B",
    )


def user_block() -> JsonObject:
    return {
        "id": "user-note",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"text": {"content": "我的手工笔记"}}]},
    }


def managed(identifier: str, marker: str) -> JsonObject:
    return {
        "id": identifier,
        "type": "toggle",
        "toggle": {"rich_text": [{"text": {"content": marker}}]},
    }


def test_complete_page_is_created_with_player_summary_mindmap_and_transcript() -> None:
    api = FakeNotion([user_block()])
    renderer = EpisodePageRenderer(api)
    result = renderer.publish(
        EpisodePageInput(
            page_id="episode-page",
            audio_url="https://cdn.example/audio.mp3",
            transcript=transcript(),
            summary=summary(),
        )
    )
    assert result.action == "created"
    assert result.managed_block_id == "new-1"
    assert api.deleted == []
    assert api.page_children[0]["id"] == "user-note"
    assert len(api.uploads) == 1
    assert api.uploads[0][1] == "image/svg+xml"
    assert api.uploads[0][2].startswith(b"<svg")

    rendered = str(api.appended)
    assert "'type': 'audio'" in rendered
    assert "全文摘要" in rendered
    assert "思维导图" in rendered
    assert "粗粒度时间轴" in rendered
    assert "00:00:00 · 主播" in rendered
    assert "展开文字版思维导图" in rendered
    assert (
        rendered.index("音频")
        < rendered.index("思维导图")
        < rendered.index("全文摘要")
        < rendered.index("章节速览")
        < rendered.index("文字稿")
        < rendered.index("关键观点")
    )
    assert block_text(api.blocks["new-1"]).startswith(f"{MANAGED_PREFIX} · READY · ")


def test_same_content_is_unchanged_and_duplicate_managed_roots_are_cleaned() -> None:
    api = FakeNotion([user_block()])
    data = EpisodePageInput("episode-page", None, transcript(), None)
    renderer = EpisodePageRenderer(api)
    first = renderer.publish(data)
    duplicate = managed("duplicate", f"{MANAGED_PREFIX} · BUILDING · old")
    api.page_children.append(duplicate)
    api.blocks["duplicate"] = duplicate

    uploads_before = len(api.uploads)
    second = renderer.publish(data)
    assert second.action == "unchanged"
    assert second.managed_block_id == first.managed_block_id
    assert api.deleted == ["duplicate"]
    assert len(api.uploads) == uploads_before
    assert any(block.get("id") == "user-note" for block in api.page_children)


def test_changed_content_publishes_new_tree_before_deleting_old_managed_root() -> None:
    old = managed("old-managed", f"{MANAGED_PREFIX} · READY · oldhash")
    api = FakeNotion([user_block(), old])
    result = EpisodePageRenderer(api).publish(
        EpisodePageInput("episode-page", None, transcript("变化后的文字"), None)
    )
    assert result.action == "updated"
    assert api.deleted == ["old-managed"]
    assert any(block.get("id") == "user-note" for block in api.page_children)
    assert any(block.get("id") == result.managed_block_id for block in api.page_children)


def test_partial_failure_keeps_old_ready_tree_and_user_notes() -> None:
    old = managed("old-managed", f"{MANAGED_PREFIX} · READY · oldhash")
    api = FakeNotion([user_block(), old])
    api.fail_after_root = True
    with pytest.raises(RuntimeError, match="injected"):
        EpisodePageRenderer(api).publish(
            EpisodePageInput("episode-page", None, transcript("new"), None)
        )
    assert api.deleted == []
    assert any(block.get("id") == "old-managed" for block in api.page_children)
    assert any(block.get("id") == "user-note" for block in api.page_children)
    assert any(
        block_text(block).startswith(f"{MANAGED_PREFIX} · BUILDING · ")
        for block in api.page_children
    )


def test_transcript_without_segments_is_split_into_complete_notion_paragraphs() -> None:
    api = FakeNotion()
    long_text = "播" * 4501
    bare = TranscriptResult(
        provider="tingwu_cookie",
        provider_task_id="task",
        model="tingwu",
        duration_ms=1,
        text=long_text,
        timing_quality=TranscriptTimingQuality.EXACT,
    )
    EpisodePageRenderer(api).publish(EpisodePageInput("episode-page", None, bare, None))
    rendered_paragraphs = [
        block
        for parent, blocks in api.appended
        if parent == "new-1"
        for block in blocks
        if block.get("type") == "paragraph"
    ]
    restored = "".join(
        item["text"]["content"]
        for block in rendered_paragraphs
        for item in block["paragraph"]["rich_text"]
    )
    assert restored == long_text


def test_missing_managed_ids_are_rejected() -> None:
    class MissingRootID(FakeNotion):
        def append_block_children(
            self,
            block_id: str,
            children: Sequence[Mapping[str, Any]],
        ) -> list[JsonObject]:
            return [{}]

    with pytest.raises(RuntimeError, match="root block ID"):
        EpisodePageRenderer(MissingRootID()).publish(
            EpisodePageInput("episode-page", None, transcript(), None)
        )
