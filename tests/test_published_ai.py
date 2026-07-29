from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from xyz2notion.models import MindmapNode, SummaryResult, TranscriptResult
from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.published_ai import PublishedAIReconciler
from xyz2notion.orchestration.state_store import EpisodeAIState
from xyz2notion.state import PipelineRecord


class FakeAPI:
    def __init__(self) -> None:
        self.mindmaps: list[JsonObject] = []

    def list_block_children(self, _block_id: str) -> list[JsonObject]:
        return [
            {
                "type": "toggle",
                "toggle": {"rich_text": [{"plain_text": "🤖 Xyz2Notion 自动内容 · READY · hash"}]},
            }
        ]

    def query_data_source(
        self,
        _data_source_id: str,
        _payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        return list(self.mindmaps)

    def create_data_source_page(
        self,
        _data_source_id: str,
        properties: Mapping[str, Any],
        *,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
        children: Sequence[Mapping[str, Any]] = (),
    ) -> JsonObject:
        page = {"id": "mindmap", "properties": dict(properties)}
        self.mindmaps.append(page)
        return page

    def update_page(self, page_id: str, _payload: Mapping[str, Any]) -> JsonObject:
        return {"id": page_id}


class FakeStore:
    def load(self, _page: Mapping[str, Any], eid: str) -> EpisodeAIState:
        return EpisodeAIState(
            record=PipelineRecord(eid=eid),
            transcript=TranscriptResult(
                provider="fixture",
                provider_task_id="task",
                model="model",
                duration_ms=1,
                text="文字稿",
            ),
            summary=SummaryResult(
                summary="摘要",
                mindmap=MindmapNode(node_id="root", title="主题"),
                prompt_version="v1",
                model="model",
            ),
            content_version="hash",
        )


class IncompleteStore:
    def load(self, _page: Mapping[str, Any], eid: str) -> EpisodeAIState:
        return EpisodeAIState(record=PipelineRecord(eid=eid))


def test_published_ai_reconciliation_audits_and_backfills_one_row() -> None:
    page = {
        "id": "episode-page",
        "properties": {
            "EID": {"rich_text": [{"plain_text": "episode"}]},
            "Name": {"title": [{"plain_text": "单集"}]},
        },
    }
    api = FakeAPI()
    report = PublishedAIReconciler(api, FakeStore(), "mindmaps").reconcile(
        [page],
        limit=1,
    )
    assert report.selected == 1
    assert report.transcripts == 1
    assert report.summaries == 1
    assert report.page_ready == 1
    assert report.mindmaps_created == 1
    assert report.incomplete == 0


def test_published_ai_reconciliation_counts_invalid_and_incomplete_rows() -> None:
    page = {
        "id": "episode-page",
        "properties": {
            "EID": {"rich_text": [{"plain_text": "episode"}]},
            "Name": {"title": [{"plain_text": "单集"}]},
        },
    }
    api = FakeAPI()
    api.list_block_children = lambda _page_id: []  # type: ignore[method-assign]
    report = PublishedAIReconciler(api, IncompleteStore(), "mindmaps").reconcile(
        [{}, page],
        limit=2,
    )
    assert report.selected == 2
    assert report.page_ready == 0
    assert report.incomplete == 2
    assert report.mindmaps_created == 0


def test_published_ai_reconciliation_rejects_more_than_two() -> None:
    with pytest.raises(ValueError, match="between 1 and 2"):
        PublishedAIReconciler(FakeAPI(), FakeStore(), "mindmaps").reconcile(
            [],
            limit=3,
        )
