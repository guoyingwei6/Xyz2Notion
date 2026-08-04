"""Notion-only audit and reconciliation for already-published Episode AI content."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.episode_page import MANAGED_PREFIX
from xyz2notion.notion.mindmap_database import MindmapDatabaseSynchronizer
from xyz2notion.orchestration.state_store import (
    EpisodeAIState,
    _enrichment_provider,
    _enrichment_status,
)


class PublishedStateStore(Protocol):
    def load(self, page: Mapping[str, Any], eid: str) -> EpisodeAIState: ...


class PublishedAIAPI(Protocol):
    def list_block_children(self, block_id: str) -> list[JsonObject]: ...

    def query_data_source(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]: ...

    def create_data_source_page(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
        *,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
        children: Sequence[Mapping[str, Any]] = (),
    ) -> JsonObject: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...


@dataclass(frozen=True)
class PublishedAIReport:
    selected: int
    transcripts: int
    summaries: int
    page_ready: int
    mindmaps_created: int
    mindmaps_updated: int
    mindmaps_unchanged: int
    incomplete: int


def _plain_text(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in items
        if isinstance(item, Mapping)
    )


def _property_text(page: Mapping[str, Any], name: str) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return ""
    return _plain_text(value.get("title") or value.get("rich_text"))


def _block_text(block: Mapping[str, Any]) -> str:
    block_type = block.get("type")
    body = block.get(block_type) if isinstance(block_type, str) else None
    return _plain_text(body.get("rich_text")) if isinstance(body, Mapping) else ""


class PublishedAIReconciler:
    """Audit existing page blocks and backfill only the standalone mind-map row."""

    def __init__(
        self,
        api: PublishedAIAPI,
        state_store: PublishedStateStore,
        mindmap_data_source_id: str,
    ) -> None:
        self.api = api
        self.state_store = state_store
        self.mindmaps = MindmapDatabaseSynchronizer(api, mindmap_data_source_id)

    def reconcile(self, pages: Sequence[JsonObject], *, limit: int) -> PublishedAIReport:
        if not 1 <= limit <= 2:
            raise ValueError("published AI reconciliation limit must be between 1 and 2")
        selected = list(pages[:limit])
        transcripts = summaries = page_ready = incomplete = 0
        actions: Counter[str] = Counter()
        for page in selected:
            page_id = str(page.get("id") or "")
            eid = _property_text(page, "EID")
            title = _property_text(page, "Name")
            if not page_id or not eid or not title:
                incomplete += 1
                continue
            state = self.state_store.load(page, eid)
            if state.transcript is not None:
                transcripts += 1
            if state.summary is not None:
                summaries += 1
            timestamps: JsonObject = {}
            if state.transcript is not None:
                timestamps["转写完成时间"] = {
                    "date": {"start": state.transcript.created_at.isoformat()}
                }
            if state.summary is not None:
                timestamps["总结完成时间"] = {
                    "date": {"start": state.summary.created_at.isoformat()}
                }
            # Backfill the independent enrichment metadata while reconciling
            # legacy published pages.  ASR status remains an ASR-only field.
            timestamps["增强 Provider"] = {
                "rich_text": (
                    [{"type": "text", "text": {"content": _enrichment_provider(state)}}]
                    if _enrichment_provider(state)
                    else []
                )
            }
            timestamps["增强状态"] = {"select": {"name": _enrichment_status(state.record)}}
            if timestamps:
                self.api.update_page(page_id, {"properties": timestamps})
            roots = self.api.list_block_children(page_id)
            if any(
                block.get("type") == "toggle"
                and _block_text(block).startswith(f"{MANAGED_PREFIX} · READY")
                for block in roots
            ):
                page_ready += 1
            if state.transcript is None or state.summary is None or not state.content_version:
                incomplete += 1
                continue
            result = self.mindmaps.sync(
                eid=eid,
                episode_page_id=page_id,
                episode_title=title,
                summary=state.summary,
                content_version=state.content_version,
            )
            actions[result.action] += 1
        return PublishedAIReport(
            selected=len(selected),
            transcripts=transcripts,
            summaries=summaries,
            page_ready=page_ready,
            mindmaps_created=actions["created"],
            mindmaps_updated=actions["updated"],
            mindmaps_unchanged=actions["unchanged"],
            incomplete=incomplete,
        )
