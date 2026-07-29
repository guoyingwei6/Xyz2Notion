from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.schema import NotionResource
from xyz2notion.sync.metadata import MetadataSynchronizer
from xyz2notion.sync.normalizer import MetadataSnapshot, build_metadata_snapshot
from xyz2notion.sync.notion_table import NotionTable


def sample_snapshot(*, progress_seconds: int = 120) -> MetadataSnapshot:
    return build_metadata_snapshot(
        subscriptions=[
            {
                "pid": "podcast-fixture-1",
                "title": "Fixture Podcast",
                "description": "Podcast description",
                "image": {"picture": {"picUrl": "https://cdn.example/podcast.jpg"}},
                "latestEpisodePubDate": "2026-07-01T00:00:00Z",
                "podcasters": [
                    {
                        "uid": "author-fixture-1",
                        "nickname": "Fixture Host",
                        "avatar": {"picture": {"picUrl": "https://cdn.example/author.jpg"}},
                    }
                ],
            }
        ],
        mileage=[
            {
                "podcast": {
                    "pid": "podcast-fixture-1",
                    "title": "Fixture Podcast",
                },
                "playedSeconds": 3600,
            }
        ],
        history=[
            {
                "episode": {
                    "eid": "episode-fixture-1",
                    "pid": "podcast-fixture-1",
                    "title": "Fixture Episode",
                    "description": "Episode description",
                    "pubDate": "2026-06-30T12:00:00Z",
                    "duration": 1800,
                    "isPicked": True,
                    "media": {"source": {"url": "https://cdn.example/episode.mp3"}},
                }
            }
        ],
        progress=[
            {
                "eid": "episode-fixture-1",
                "progress": progress_seconds,
                "playedAt": "2026-07-02T08:00:00Z",
            }
        ],
        now=datetime(2026, 7, 3, tzinfo=UTC),
    )


class FakeRows:
    def __init__(self) -> None:
        self.pages: dict[str, list[JsonObject]] = {}
        self.created = 0
        self.updates: list[tuple[str, JsonObject]] = []

    def query_data_source(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        assert payload == {"page_size": 100}
        return list(self.pages.get(data_source_id, []))

    def create_data_source_page(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
        *,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
        children: Sequence[Mapping[str, Any]] = (),
    ) -> JsonObject:
        self.created += 1
        page = {
            "id": f"page-{self.created}",
            "parent": {
                "type": "data_source_id",
                "data_source_id": data_source_id,
            },
            "properties": dict(properties),
            "icon": dict(icon) if icon else None,
            "cover": dict(cover) if cover else None,
            "children": [dict(child) for child in children],
        }
        self.pages.setdefault(data_source_id, []).append(page)
        return page

    def update_page(
        self,
        page_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        update = dict(payload)
        self.updates.append((page_id, update))
        page = self.page(page_id)
        properties = update.get("properties")
        if isinstance(properties, Mapping):
            page["properties"].update(properties)
        for key in ("icon", "cover"):
            if key in update:
                page[key] = update[key]
        return page

    def page(self, page_id: str) -> JsonObject:
        return next(
            page for pages in self.pages.values() for page in pages if page["id"] == page_id
        )


def resources() -> dict[str, NotionResource]:
    return {
        key: NotionResource(database_id=f"db-{key}", data_source_id=f"ds-{key}")
        for key in (
            "author",
            "podcast",
            "episode",
            "all",
            "year",
            "month",
            "week",
            "day",
            "mindmap",
        )
    }


def page_by_key(
    fake: FakeRows,
    data_source_id: str,
    property_name: str,
    value: str,
) -> JsonObject:
    for page in fake.pages[data_source_id]:
        items = page["properties"][property_name].get("rich_text")
        if items is None:
            items = page["properties"][property_name].get("title")
        rendered = "".join(item["text"]["content"] for item in items)
        if rendered == value:
            return page
    raise AssertionError(value)


def test_normalizer_merges_metadata_without_mutating_source() -> None:
    source = [{"podcast": {"pid": "podcast-fixture-1"}, "playedSeconds": 3600}]
    snapshot = build_metadata_snapshot(
        subscriptions=[
            {
                "pid": "podcast-fixture-1",
                "title": "Fixture",
                "podcasters": [{"nickname": "Name-only Host"}],
            }
        ],
        mileage=source,
        history=[],
        progress=[],
        now=datetime(2026, 7, 3, tzinfo=UTC),
    )
    assert snapshot.podcasts[0].total_listening_seconds == 3600
    assert snapshot.authors[0].author_id.startswith("name-sha256:")
    assert source == [{"podcast": {"pid": "podcast-fixture-1"}, "playedSeconds": 3600}]


def test_normalizer_handles_numeric_dates_progress_clamp_and_direct_images() -> None:
    snapshot = build_metadata_snapshot(
        subscriptions=[
            {
                "pid": "podcast-numeric",
                "title": "Numeric",
                "image": {"picUrl": "https://cdn.example/direct.jpg"},
                "latestEpisodePubDate": 1_700_000_000,
                "podcasters": [
                    {
                        "authorId": "author-direct",
                        "name": "Direct Author",
                        "bio": "Bio",
                        "avatar": {"picUrl": "https://cdn.example/avatar.jpg"},
                    }
                ],
            }
        ],
        mileage=[],
        history=[
            {
                "eid": "episode-numeric",
                "pid": "podcast-numeric",
                "title": "Numeric Episode",
                "pubDate": 1_700_000_000,
                "duration": 100,
                "image": {"picUrl": "https://cdn.example/episode.jpg"},
            },
            {"eid": "", "pid": "ignored"},
        ],
        progress=[{"eid": "episode-numeric", "progress": 999}],
        now=datetime(2026, 7, 3, tzinfo=UTC),
    )
    assert snapshot.authors[0].author_id == "author-direct"
    assert snapshot.authors[0].bio == "Bio"
    assert snapshot.podcasts[0].image_url == "https://cdn.example/direct.jpg"
    assert snapshot.episodes[0].played_seconds == 100
    assert snapshot.episodes[0].listening_status.value == "played"


def test_first_and_second_sync_are_idempotent() -> None:
    fake = FakeRows()
    synchronizer = MetadataSynchronizer(fake, resources())
    first = synchronizer.sync(sample_snapshot())
    assert first.created == 8  # author + podcast + episode + five periods
    assert first.updated == 0
    assert first.unchanged == 0

    second = synchronizer.sync(sample_snapshot())
    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 8
    assert fake.created == 8

    episode = page_by_key(
        fake,
        "ds-episode",
        "EID",
        "episode-fixture-1",
    )
    assert episode["properties"]["ASR Status"]["select"]["name"] == "待处理"
    assert episode["properties"]["Podcast"]["relation"] == [{"id": "page-2"}]
    assert set(name for name in episode["properties"] if name.endswith("Period")) == {
        "All Period",
        "Year Period",
        "Month Period",
        "Week Period",
        "Day Period",
    }


def test_progress_change_updates_only_changed_managed_field() -> None:
    fake = FakeRows()
    synchronizer = MetadataSynchronizer(fake, resources())
    synchronizer.sync(sample_snapshot(progress_seconds=120))
    fake.updates.clear()

    report = synchronizer.sync(sample_snapshot(progress_seconds=130))
    assert report.created == 0
    assert report.updated == 1
    assert report.unchanged == 7
    assert report.changed_fields == {"Played Seconds": 1}
    assert len(fake.updates) == 1
    assert set(fake.updates[0][1]["properties"]) == {"Played Seconds"}


def test_user_fields_blocks_and_processed_asr_status_are_preserved() -> None:
    fake = FakeRows()
    synchronizer = MetadataSynchronizer(fake, resources())
    snapshot = sample_snapshot()
    synchronizer.sync(snapshot)
    episode = page_by_key(fake, "ds-episode", "EID", "episode-fixture-1")
    episode["properties"]["My Notes"] = {"rich_text": [{"text": {"content": "用户笔记"}}]}
    episode["properties"]["ASR Status"] = {"select": {"name": "已转写"}}
    episode["children"] = [{"type": "paragraph", "text": "用户页面块"}]

    synchronizer.sync(snapshot)
    assert episode["properties"]["My Notes"]["rich_text"][0]["text"]["content"] == ("用户笔记")
    assert episode["properties"]["ASR Status"]["select"]["name"] == "已转写"
    assert episode["children"] == [{"type": "paragraph", "text": "用户页面块"}]


def test_notion_table_compares_real_response_shapes_and_updates_visuals() -> None:
    fake = FakeRows()
    fake.pages["ds-custom"] = [
        {
            "id": "existing",
            "properties": {
                "Key": {"type": "rich_text", "rich_text": [{"plain_text": "stable"}]},
                "Name": {"type": "title", "title": [{"plain_text": "Same"}]},
                "Count": {"type": "number", "number": 1},
                "Flag": {"type": "checkbox", "checkbox": True},
                "URL": {"type": "url", "url": "https://example.com"},
                "Status": {"type": "select", "select": {"name": "在听"}},
                "Date": {
                    "type": "date",
                    "date": {"start": "2026-01-01", "end": None},
                },
                "Links": {
                    "type": "relation",
                    "relation": [{"id": "two"}, {"id": "one"}],
                },
                "Files": {
                    "type": "files",
                    "files": [
                        {
                            "name": "cover",
                            "type": "file",
                            "file": {"url": "https://cdn.example/cover.jpg"},
                        }
                    ],
                },
            },
            "icon": {"type": "external", "external": {"url": "https://old/icon"}},
            "cover": {
                "type": "external",
                "external": {"url": "https://old/cover"},
            },
        }
    ]
    table = NotionTable(fake, "ds-custom", "Key")
    result = table.upsert(
        "stable",
        {
            "Key": {"rich_text": [{"text": {"content": "stable"}}]},
            "Name": {"title": [{"text": {"content": "Same"}}]},
            "Count": {"number": 1},
            "Flag": {"checkbox": True},
            "URL": {"url": "https://example.com"},
            "Status": {"select": {"name": "在听"}},
            "Date": {"date": {"start": "2026-01-01", "end": None}},
            "Links": {"relation": [{"id": "one"}, {"id": "two"}]},
            "Files": {
                "files": [
                    {
                        "name": "cover",
                        "type": "external",
                        "external": {"url": "https://cdn.example/cover.jpg"},
                    }
                ]
            },
        },
        icon={"type": "external", "external": {"url": "https://new/icon"}},
        cover={"type": "external", "external": {"url": "https://new/cover"}},
    )
    assert result.action == "updated"
    assert result.changed_properties == ()
    assert fake.updates[0][1] == {
        "icon": {"type": "external", "external": {"url": "https://new/icon"}},
        "cover": {"type": "external", "external": {"url": "https://new/cover"}},
    }


def test_notion_table_rejects_duplicate_stable_keys() -> None:
    fake = FakeRows()
    fake.pages["ds-duplicate"] = [
        {
            "id": f"page-{index}",
            "properties": {
                "PID": {"rich_text": [{"plain_text": "duplicate"}]},
            },
        }
        for index in range(2)
    ]
    try:
        NotionTable(fake, "ds-duplicate", "PID")
    except ValueError as exc:
        assert "Duplicate Notion key" in str(exc)
    else:
        raise AssertionError("duplicate key was not rejected")
