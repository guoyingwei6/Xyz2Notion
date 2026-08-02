"""Idempotently mirror Episode mind maps into the standalone Notion database."""

from __future__ import annotations

from datetime import datetime

from xyz2notion.models import MindmapNode, SummaryResult
from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.sync.notion_table import NotionRowsAPI, NotionTable, UpsertResult


def _text(value: str) -> JsonObject:
    return {"rich_text": rich_text(value)}


def _title(value: str) -> JsonObject:
    return {"title": rich_text(value)}


def _mermaid_label(value: str) -> str:
    return " ".join(value.replace('"', "'").replace("(", "[").replace(")", "]").split())


def _mermaid_lines(node: MindmapNode, *, depth: int = 1) -> list[str]:
    lines = [f'{"  " * depth}"{_mermaid_label(node.title)}"']
    for child in node.children:
        lines.extend(_mermaid_lines(child, depth=depth + 1))
    return lines


def render_mindmap_mermaid(node: MindmapNode) -> str:
    """Return a deterministic Mermaid mindmap representation."""
    return "\n".join(("mindmap", *_mermaid_lines(node)))


class MindmapDatabaseSynchronizer:
    """Maintain exactly one standalone mind-map row per Episode EID."""

    def __init__(self, api: NotionRowsAPI, data_source_id: str) -> None:
        self.table = NotionTable(api, data_source_id, "Mindmap Key")

    def sync(
        self,
        *,
        eid: str,
        episode_page_id: str,
        episode_title: str,
        summary: SummaryResult,
        content_version: str,
    ) -> UpsertResult:
        updated_at: datetime = summary.created_at
        return self.table.upsert(
            eid,
            {
                "Name": _title(episode_title),
                "Mindmap Key": _text(eid),
                "Mindmap JSON": _text(summary.mindmap.model_dump_json()),
                "Mermaid": _text(render_mindmap_mermaid(summary.mindmap)),
                "Content Version": _text(content_version),
                "Updated At": {"date": {"start": updated_at.isoformat()}},
                "总结完成时间": {"date": {"start": updated_at.isoformat()}},
                "Episode": {"relation": [{"id": episode_page_id}]},
            },
        )
