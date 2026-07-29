from collections.abc import Mapping
from typing import Any

from xyz2notion.migration.legacy import LegacyTemplateMigrator
from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.schema import NotionResource


def _title(value: str) -> JsonObject:
    return {"type": "title", "title": [{"plain_text": value}]}


def _text(value: str) -> JsonObject:
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


class FakeMigrationAPI:
    def __init__(self) -> None:
        self.rows: dict[str, list[JsonObject]] = {
            "ds-author": [
                {
                    "id": "author-page",
                    "properties": {
                        "标题": _title("主持人"),
                        "用户备注": _text("不要覆盖"),
                    },
                }
            ],
            "ds-podcast": [
                {
                    "id": "podcast-page",
                    "properties": {
                        "播客": _title("测试播客"),
                        "Pid": _text("pid-1"),
                        "Description": _text("播客简介"),
                        "链接": {"url": "hhttps://www.xiaoyuzhoufm.com/podcast/pid-1"},
                        "收听时长": {"number": 3600},
                        "最后更新时间": {"date": {"start": "2026-01-02"}},
                        "作者": {"relation": [{"id": "author-page"}]},
                        "用户备注": _text("保留"),
                    },
                }
            ],
            "ds-episode": [
                {
                    "id": "episode-page",
                    "properties": {
                        "标题": _title("测试单集"),
                        "Eid": _text("eid-1"),
                        "音频": {"url": "https://audio.example/1.mp3"},
                        "链接": {"url": "hhttps://www.xiaoyuzhoufm.com/episode/eid-1"},
                        "状态": {"select": {"name": "听过"}},
                        "喜欢": {"checkbox": True},
                        "时长": {"number": 120},
                        "收听进度": {"number": 120},
                        "发布时间": {"date": {"start": "2026-01-01"}},
                        "语音转文字状态": {"status": {"name": "Done"}},
                        "通义链接": {
                            "url": "https://tongyi.aliyun.com/efficiency/doc/transcripts/task-1"
                        },
                        "用户笔记": _text("正文和属性都必须保留"),
                    },
                }
            ],
        }
        self.blocks: dict[str, list[JsonObject]] = {
            "root": [
                {
                    "id": "heatmap",
                    "type": "embed",
                    "embed": {"url": "https://heatmap.malinkang.com/?image=x"},
                },
                {
                    "id": "user-link",
                    "type": "embed",
                    "embed": {"url": "https://example.com/user"},
                },
            ],
            "episode-page": [
                {
                    "id": "nested",
                    "type": "toggle",
                    "has_children": True,
                    "toggle": {"rich_text": rich_text("旧自动内容")},
                },
                {
                    "id": "user-note",
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text("我的笔记")},
                },
            ],
            "nested": [
                {
                    "id": "old-player",
                    "type": "embed",
                    "embed": {"url": "https://notion-music.malinkang.com/player?x=1"},
                },
            ],
        }
        self.updates: list[tuple[str, Mapping[str, Any]]] = []
        self.deleted: list[str] = []

    def query_data_source(
        self,
        data_source_id: str,
        _payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        return list(self.rows.get(data_source_id, []))

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.updates.append((page_id, payload))
        return {"id": page_id}

    def list_block_children(self, block_id: str) -> list[JsonObject]:
        return list(self.blocks.get(block_id, []))

    def delete_block(self, block_id: str) -> JsonObject:
        self.deleted.append(block_id)
        return {"id": block_id, "in_trash": True}


RESOURCES = {
    "author": NotionResource("db-author", "ds-author"),
    "podcast": NotionResource("db-podcast", "ds-podcast"),
    "episode": NotionResource("db-episode", "ds-episode"),
}


def test_dry_run_plans_without_mutating_any_page_or_block() -> None:
    api = FakeMigrationAPI()
    report = LegacyTemplateMigrator(api, RESOURCES, "root").migrate(dry_run=True)

    assert report.scanned_pages == 3
    assert report.planned_updates == 3
    assert report.legacy_embeds_found == 2
    assert report.updated_pages == 0
    assert report.legacy_embeds_removed == 0
    assert api.updates == []
    assert api.deleted == []


def test_migration_updates_same_page_ids_and_only_removes_known_service_embeds() -> None:
    api = FakeMigrationAPI()
    report = LegacyTemplateMigrator(api, RESOURCES, "root").migrate(dry_run=False)

    assert report.updated_pages == 3
    assert {page_id for page_id, _payload in api.updates} == {
        "author-page",
        "podcast-page",
        "episode-page",
    }
    episode_payload = next(payload for page_id, payload in api.updates if page_id == "episode-page")
    episode_properties = episode_payload["properties"]
    assert episode_properties["EID"]["rich_text"][0]["text"]["content"] == "eid-1"
    assert episode_properties["ASR Status"]["select"]["name"] == "已发布"
    assert episode_properties["ASR Task ID"]["rich_text"][0]["text"]["content"] == "task-1"
    assert "用户笔记" not in episode_properties
    podcast_payload = next(payload for page_id, payload in api.updates if page_id == "podcast-page")
    podcast_properties = podcast_payload["properties"]
    assert podcast_properties["URL"]["url"].startswith("https://")
    assert podcast_properties["Updated At"]["date"]["start"] == "2026-01-02"
    assert podcast_properties["Authors"]["relation"] == [{"id": "author-page"}]
    assert set(api.deleted) == {"heatmap", "old-player"}
    assert "user-link" not in api.deleted
    assert "user-note" not in api.deleted


def test_duplicate_pid_stops_all_writes() -> None:
    api = FakeMigrationAPI()
    duplicate = {
        "id": "podcast-duplicate",
        "properties": {
            "播客": _title("重复播客"),
            "Pid": _text("pid-1"),
        },
    }
    api.rows["ds-podcast"].append(duplicate)

    report = LegacyTemplateMigrator(api, RESOURCES, "root").migrate(dry_run=False)

    assert report.duplicate_keys == ("podcast:pid-1",)
    assert report.updated_pages == 0
    assert api.updates == []
    assert api.deleted == []
