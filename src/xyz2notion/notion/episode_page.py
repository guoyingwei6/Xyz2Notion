"""Idempotent Episode page rendering inside one program-owned root block."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from xyz2notion.models import (
    MindmapNode,
    SummaryResult,
    TranscriptResult,
    TranscriptTimingQuality,
)
from xyz2notion.notion.client import JsonObject, paragraph_blocks, rich_text
from xyz2notion.notion.mindmap import render_mindmap_svg

MANAGED_PREFIX = "🤖 Xyz2Notion 自动内容"


class EpisodePageAPI(Protocol):
    def list_block_children(self, block_id: str) -> list[JsonObject]: ...

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]: ...

    def update_block(
        self,
        block_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject: ...

    def delete_block(self, block_id: str) -> JsonObject: ...

    def upload_file(
        self,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> str: ...


@dataclass(frozen=True)
class EpisodePageInput:
    """All persisted data required to render one Episode page."""

    page_id: str
    audio_url: str | None
    transcript: TranscriptResult
    summary: SummaryResult | None = None


@dataclass(frozen=True)
class EpisodePagePublishResult:
    action: str
    content_hash: str
    managed_block_id: str


def _block_text(block: Mapping[str, Any]) -> str:
    block_type = block.get("type")
    body = block.get(block_type) if isinstance(block_type, str) else None
    if not isinstance(body, Mapping):
        return ""
    values = body.get("rich_text")
    if not isinstance(values, list):
        return ""
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in values
        if isinstance(item, Mapping)
    )


def _heading(level: int, text: str) -> JsonObject:
    block_type = f"heading_{level}"
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich_text(text)},
    }


def _callout(text: str, emoji: str = "💡") -> JsonObject:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text(text),
            "icon": {"type": "emoji", "emoji": emoji},
            "color": "gray_background",
        },
    }


def _list_block(text: str) -> JsonObject:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich_text(text)},
    }


def _quote(text: str) -> JsonObject:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": rich_text(text)},
    }


def _divider() -> JsonObject:
    return {
        "object": "block",
        "type": "divider",
        "divider": {},
    }


def _time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _content_hash(data: EpisodePageInput) -> str:
    digest = hashlib.sha256()
    digest.update((data.audio_url or "").encode())
    digest.update(data.transcript.model_dump_json().encode())
    if data.summary is not None:
        digest.update(data.summary.model_dump_json().encode())
    return digest.hexdigest()[:20]


def _marker(state: str, content_hash: str) -> str:
    return f"{MANAGED_PREFIX} · {state} · {content_hash}"


def _toggle(marker: str) -> JsonObject:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": rich_text(marker),
            "color": "default",
        },
    }


def _summary_blocks(summary: SummaryResult) -> list[JsonObject]:
    blocks: list[JsonObject] = [_heading(2, "全文摘要"), _callout(summary.summary)]
    if summary.chapters:
        blocks.append(_heading(2, "章节速览"))
        for chapter in summary.chapters:
            blocks.append(_heading(3, f"{_time(chapter.start_ms)} {chapter.title}"))
            if chapter.summary:
                blocks.append(_callout(chapter.summary))
    return blocks


def _insight_blocks(summary: SummaryResult) -> list[JsonObject]:
    """Render useful secondary analysis after the screenshot-critical main flow."""
    blocks: list[JsonObject] = []
    if summary.highlights:
        blocks.append(_heading(2, "关键观点"))
        blocks.extend(_list_block(value) for value in summary.highlights)
    if summary.quotes:
        blocks.append(_heading(2, "原文金句"))
        blocks.extend(_quote(value) for value in summary.quotes)
    if summary.terms:
        blocks.append(_heading(2, "术语"))
        blocks.extend(_list_block(value) for value in summary.terms)
    if summary.people:
        blocks.append(_heading(2, "人物"))
        blocks.extend(_list_block(value) for value in summary.people)
    if summary.questions:
        blocks.append(_heading(2, "问题回顾"))
        blocks.extend(_callout(value, "❓") for value in summary.questions)
    return blocks


def _transcript_blocks(transcript: TranscriptResult) -> list[JsonObject]:
    blocks: list[JsonObject] = [_heading(2, "文字稿")]
    if transcript.timing_quality is TranscriptTimingQuality.COARSE:
        blocks.append(
            _callout(
                "SiliconFlow 仅提供分片级粗粒度时间轴, 时间点不代表逐句精确位置。",
                "⚠️",
            )
        )
    if transcript.segments:
        for segment in transcript.segments:
            speaker = f" · {segment.speaker}" if segment.speaker else ""
            row = f"{_time(segment.start_ms)}{speaker}\n{segment.text}"
            blocks.extend(paragraph_blocks(row))
    else:
        blocks.extend(paragraph_blocks(transcript.text))
    return blocks


def _mindmap_block(title: str) -> JsonObject:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich_text(title)},
    }


class EpisodePageRenderer:
    """Publish a new complete managed subtree, then archive only old managed roots."""

    def __init__(self, api: EpisodePageAPI) -> None:
        self.api = api

    def _managed_roots(self, page_id: str) -> list[JsonObject]:
        return [
            block
            for block in self.api.list_block_children(page_id)
            if block.get("type") == "toggle" and _block_text(block).startswith(MANAGED_PREFIX)
        ]

    def _append_mindmap(self, parent_id: str, node: MindmapNode) -> None:
        created = self.api.append_block_children(parent_id, [_mindmap_block(node.title)])
        if not created or not created[0].get("id"):
            raise RuntimeError("Notion did not return a mind-map block ID")
        node_id = str(created[0]["id"])
        if not node.children:
            return
        child_blocks = [_mindmap_block(child.title) for child in node.children]
        child_results = self.api.append_block_children(node_id, child_blocks)
        if len(child_results) != len(node.children):
            raise RuntimeError("Notion returned incomplete mind-map child IDs")
        for child, result in zip(node.children, child_results, strict=True):
            child_id = result.get("id")
            if not child_id:
                raise RuntimeError("Notion omitted a mind-map child ID")
            for grandchild in child.children:
                self._append_mindmap(str(child_id), grandchild)

    def _append_native_mindmap(self, parent_id: str, node: MindmapNode) -> None:
        """Keep the accessible outline available without duplicating the SVG in full."""
        created = self.api.append_block_children(
            parent_id,
            [
                {
                    "object": "block",
                    "type": "toggle",
                    "toggle": {
                        "rich_text": rich_text("展开文字版思维导图"),
                        "color": "gray_background",
                    },
                }
            ],
        )
        if not created or not created[0].get("id"):
            raise RuntimeError("Notion did not return the native mind-map toggle ID")
        self._append_mindmap(str(created[0]["id"]), node)

    def publish(self, data: EpisodePageInput) -> EpisodePagePublishResult:
        content_hash = _content_hash(data)
        ready_marker = _marker("READY", content_hash)
        existing = self._managed_roots(data.page_id)
        exact = [block for block in existing if _block_text(block) == ready_marker]
        if exact:
            keeper_id = str(exact[0].get("id") or "")
            if not keeper_id:
                raise RuntimeError("Managed Notion block has no ID")
            for duplicate in existing:
                duplicate_id = str(duplicate.get("id") or "")
                if duplicate_id and duplicate_id != keeper_id:
                    self.api.delete_block(duplicate_id)
            return EpisodePagePublishResult("unchanged", content_hash, keeper_id)

        created = self.api.append_block_children(
            data.page_id,
            [_toggle(_marker("BUILDING", content_hash))],
        )
        if not created or not created[0].get("id"):
            raise RuntimeError("Notion did not return the managed root block ID")
        managed_id = str(created[0]["id"])

        prefix: list[JsonObject] = []
        if data.audio_url:
            prefix.extend(
                [
                    _heading(2, "音频"),
                    {
                        "object": "block",
                        "type": "audio",
                        "audio": {
                            "type": "external",
                            "external": {"url": data.audio_url},
                        },
                    },
                ]
            )
        if data.summary is not None:
            prefix.append(_heading(2, "思维导图"))
            svg = render_mindmap_svg(data.summary.mindmap)
            upload_id = self.api.upload_file(
                f"xyz2notion-mindmap-{content_hash}.svg",
                "image/svg+xml",
                svg,
            )
            prefix.append(
                {
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": upload_id},
                        "caption": rich_text(f"{MANAGED_PREFIX} · MINDMAP · {content_hash}"),
                    },
                }
            )
        if prefix:
            self.api.append_block_children(managed_id, prefix)
        if data.summary is not None:
            self._append_native_mindmap(managed_id, data.summary.mindmap)
            self.api.append_block_children(managed_id, _summary_blocks(data.summary))
        self.api.append_block_children(managed_id, _transcript_blocks(data.transcript))
        if data.summary is not None:
            insights = _insight_blocks(data.summary)
            if insights:
                self.api.append_block_children(managed_id, [_divider(), *insights])
        quality = data.transcript.timing_quality.value
        self.api.append_block_children(
            managed_id,
            [
                _divider(),
                _callout(
                    f"转写信息 · {data.transcript.provider} · {data.transcript.model} · {quality}",
                    "🎙️",
                ),
            ],
        )
        self.api.update_block(
            managed_id,
            {"toggle": {"rich_text": rich_text(ready_marker), "color": "default"}},
        )
        for old in existing:
            old_id = str(old.get("id") or "")
            if old_id:
                self.api.delete_block(old_id)
        action = "updated" if existing else "created"
        return EpisodePagePublishResult(action, content_hash, managed_id)
