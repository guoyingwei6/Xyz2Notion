import struct
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from xyz2notion.models import Episode, ListeningStatus, Podcast
from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.initializer import HOME_SUMMARY_MARKER_URL
from xyz2notion.notion.schema import NotionResource
from xyz2notion.statistics.calculator import (
    MonthlyWrappedValue,
    calculate_statistics,
)
from xyz2notion.statistics.heatmap import render_heatmap_png, render_heatmap_svg
from xyz2notion.statistics.notion_sync import HeatmapPublisher, StatisticsSynchronizer
from xyz2notion.sync.normalizer import MetadataSnapshot


def statistics_snapshot() -> MetadataSnapshot:
    podcasts = (
        Podcast(
            pid="p1",
            title="First",
            total_listening_seconds=3600,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Podcast(
            pid="p2",
            title="Second",
            total_listening_seconds=1800,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    episodes = (
        Episode(
            eid="e1",
            pid="p1",
            title="One",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            duration_seconds=1000,
            played_seconds=600,
            listening_status=ListeningStatus.LISTENING,
            last_played_at=datetime(2026, 1, 2, 8, tzinfo=UTC),
        ),
        Episode(
            eid="e2",
            pid="p1",
            title="Two",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            duration_seconds=1000,
            played_seconds=300,
            listening_status=ListeningStatus.LISTENING,
            last_played_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
        ),
        Episode(
            eid="e3",
            pid="p2",
            title="Three",
            published_at=datetime(2026, 2, 1, tzinfo=UTC),
            duration_seconds=1200,
            played_seconds=1200,
            listening_status=ListeningStatus.PLAYED,
            last_played_at=datetime(2026, 2, 1, 10, tzinfo=UTC),
        ),
    )
    return MetadataSnapshot(authors=(), podcasts=podcasts, episodes=episodes)


def test_statistics_use_mileage_total_and_historical_monthly_correction() -> None:
    result = calculate_statistics(
        statistics_snapshot(),
        (
            MonthlyWrappedValue(
                year=2026,
                month=1,
                listening_seconds=1500,
                played_days=3,
            ),
            # Current-month wrapped values must not replace real-time episode data.
            MonthlyWrappedValue(
                year=2026,
                month=2,
                listening_seconds=9999,
                played_days=20,
            ),
        ),
        today=date(2026, 2, 15),
    )
    assert result.total.listening_seconds == 5400
    assert result.total.source == "mileage"
    assert [(item.pid, item.listening_seconds) for item in result.ranking] == [
        ("p1", 3600),
        ("p2", 1800),
    ]
    january, february = result.months
    assert (january.listening_seconds, january.played_days, january.source) == (
        1500,
        3,
        "monthly_wrapped",
    )
    assert (february.listening_seconds, february.played_days, february.source) == (
        1200,
        1,
        "episodes",
    )
    assert result.years[0].listening_seconds == 2700
    assert result.years[0].source == "mixed"


def test_daily_levels_dates_and_iso_week_are_deterministic() -> None:
    result = calculate_statistics(
        statistics_snapshot(),
        today=date(2026, 2, 15),
    )
    rendered = [(item.day.isoformat(), item.listening_seconds, item.level) for item in result.daily]
    assert rendered == [
        ("2026-01-02", 900, 3),
        ("2026-02-01", 1200, 4),
    ]
    assert {item.key for item in result.weeks} == {"2026-W01", "2026-W05"}
    assert {item.key for item in result.days} == {"2026-01-02", "2026-02-01"}


def test_unplayed_playlist_and_favorite_do_not_affect_statistics() -> None:
    snapshot = statistics_snapshot()
    queued = Episode(
        eid="queued",
        pid="p3",
        title="Queued",
        published_at=datetime(2026, 2, 2, tzinfo=UTC),
        played_seconds=0,
        in_playlist=True,
        favorited=True,
    )
    extra_podcast = Podcast(
        pid="p3",
        title="Queue only",
        total_listening_seconds=0,
        updated_at=datetime(2026, 2, 2, tzinfo=UTC),
    )
    result = calculate_statistics(
        replace(
            snapshot,
            podcasts=(*snapshot.podcasts, extra_podcast),
            episodes=(*snapshot.episodes, queued),
        ),
        today=date(2026, 2, 15),
    )
    assert result.total.episode_count == 3
    assert result.total.podcast_count == 2
    assert all(item.pid != "p3" for item in result.ranking)
    assert all(item.episode_count <= 2 for item in result.daily)


def test_heatmap_svg_and_png_cover_every_date_without_external_assets() -> None:
    daily = calculate_statistics(
        statistics_snapshot(),
        today=date(2026, 2, 15),
    ).daily
    svg = render_heatmap_svg(2026, daily)
    assert svg.startswith("<svg")
    assert svg.count("<rect ") == 366  # background + 365 calendar days
    assert 'data-date="2026-01-02"' in svg
    assert 'data-seconds="900"' in svg
    assert 'data-level="3"' in svg
    assert ">Jan</text>" in svg
    assert ">Dec</text>" in svg
    assert ">Mon</text>" in svg
    assert ">Wed</text>" in svg
    assert ">Fri</text>" in svg

    png = render_heatmap_png(2026, daily)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert width > 530
    assert height > 75
    assert png.endswith(b"IEND\xaeB`\x82")


def test_leap_year_svg_contains_366_calendar_cells() -> None:
    svg = render_heatmap_svg(2024, ())
    assert svg.count("<rect ") == 367  # background + leap-year days
    assert 'data-date="2024-02-29"' in svg


class FakeStatisticsNotion:
    def __init__(self) -> None:
        self.pages: dict[str, list[JsonObject]] = {}
        self.blocks: dict[str, list[JsonObject]] = {"root": []}
        self.created_pages = 0
        self.uploads = 0

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
        del icon, cover, children
        self.created_pages += 1
        page = {
            "id": f"page-{self.created_pages}",
            "properties": dict(properties),
        }
        self.pages.setdefault(data_source_id, []).append(page)
        return page

    def update_page(
        self,
        page_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        page = next(
            page for pages in self.pages.values() for page in pages if page["id"] == page_id
        )
        page["properties"].update(payload.get("properties", {}))
        return page

    def list_block_children(self, block_id: str) -> list[JsonObject]:
        return list(self.blocks.get(block_id, []))

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]:
        target = self.blocks.setdefault(block_id, [])
        created = [
            {"id": f"{block_id}-block-{len(target) + index}", **dict(child)}
            for index, child in enumerate(children, start=1)
        ]
        target.extend(created)
        return created

    def update_block(
        self,
        block_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        block = next(
            block for blocks in self.blocks.values() for block in blocks if block["id"] == block_id
        )
        block.update(payload)
        return block

    def upload_file(
        self,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> str:
        assert filename.endswith(".png")
        assert content_type == "image/png"
        assert content.startswith(b"\x89PNG")
        self.uploads += 1
        return f"upload-{self.uploads}"


def notion_resources() -> dict[str, NotionResource]:
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


def test_statistics_notion_sync_is_idempotent_and_writes_rank_and_source() -> None:
    fake = FakeStatisticsNotion()
    fake.blocks["root"].append(
        {
            "id": "summary",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "\u200b",
                            "link": {"url": HOME_SUMMARY_MARKER_URL},
                        },
                    }
                ]
            },
        }
    )
    statistics = calculate_statistics(
        statistics_snapshot(),
        (
            MonthlyWrappedValue(
                year=2026,
                month=1,
                listening_seconds=1500,
                played_days=3,
            ),
        ),
        today=date(2026, 2, 15),
    )
    synchronizer = StatisticsSynchronizer(fake, notion_resources(), root_page_id="root")
    first = synchronizer.sync(statistics, today=date(2026, 2, 15))
    assert first.created == 10
    assert first.updated == 0
    second = synchronizer.sync(statistics, today=date(2026, 2, 15))
    assert second.created == 0
    assert second.unchanged == 10
    january = next(
        page
        for page in fake.pages["ds-month"]
        if page["properties"]["Period Key"]["rich_text"][0]["text"]["content"] == "2026-01"
    )
    assert (
        january["properties"]["Statistics Source"]["rich_text"][0]["text"]["content"]
        == "monthly_wrapped"
    )
    assert january["properties"]["收听小时"]["number"] == 0.4
    first_rank = next(
        page
        for page in fake.pages["ds-podcast"]
        if page["properties"]["PID"]["rich_text"][0]["text"]["content"] == "p1"
    )
    assert first_rank["properties"]["Rank"]["number"] == 1
    assert "累计收听" in str(fake.blocks["root"][0])
    assert "3 期" in str(fake.blocks["root"][0])
    assert HOME_SUMMARY_MARKER_URL in str(fake.blocks["root"][0])


def test_statistics_notion_sync_recovers_exact_unmarked_summary_placeholder() -> None:
    fake = FakeStatisticsNotion()
    fake.blocks["root"].append(
        {
            "id": "summary",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "plain_text": "🎧 收听数据将在首次统计同步后显示。",
                        "text": {"content": "🎧 收听数据将在首次统计同步后显示。"},
                    }
                ]
            },
        }
    )
    statistics = calculate_statistics(
        statistics_snapshot(),
        today=date(2026, 2, 15),
    )

    StatisticsSynchronizer(fake, notion_resources(), root_page_id="root").sync(
        statistics,
        today=date(2026, 2, 15),
    )

    assert "累计收听" in str(fake.blocks["root"][0])
    assert HOME_SUMMARY_MARKER_URL in str(fake.blocks["root"][0])


def test_heatmap_publisher_creates_skips_and_updates_one_managed_block() -> None:
    fake = FakeStatisticsNotion()
    daily = calculate_statistics(
        statistics_snapshot(),
        today=date(2026, 2, 15),
    ).daily
    publisher = HeatmapPublisher(fake, "root")
    first = publisher.publish(2026, daily)
    assert first.action == "created"
    assert fake.uploads == 1
    assert len(fake.blocks["root"]) == 1
    assert "https://xyz2notion.local/heatmap/2026/" in str(
        fake.blocks["root"][0]["image"]["caption"]
    )

    second = publisher.publish(2026, daily)
    assert second.action == "unchanged"
    assert fake.uploads == 1
    assert len(fake.blocks["root"]) == 1

    changed = (replace(daily[0], listening_seconds=901, level=4), *daily[1:])
    third = publisher.publish(2026, changed)
    assert third.action == "updated"
    assert fake.uploads == 2
    assert len(fake.blocks["root"]) == 1


def test_heatmap_is_nested_in_podcast_record_column() -> None:
    fake = FakeStatisticsNotion()
    fake.blocks["root"] = [
        {
            "id": "columns",
            "type": "column_list",
            "has_children": True,
            "column_list": {},
        }
    ]
    fake.blocks["columns"] = [{"id": "right", "type": "column", "has_children": True, "column": {}}]
    fake.blocks["right"] = [
        {
            "id": "record-heading",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "播客记录"}}]},
        }
    ]
    daily = calculate_statistics(statistics_snapshot(), today=date(2026, 2, 15)).daily

    result = HeatmapPublisher(fake, "root").publish(2026, daily)

    assert result.action == "created"
    assert len(fake.blocks["right"]) == 2
    assert fake.blocks["right"][-1]["type"] == "image"
    assert fake.blocks["right"][-1]["image"]["caption"][0]["text"]["content"] == "\u200b"


def test_legacy_heatmap_caption_migrates_once_then_stays_idempotent() -> None:
    fake = FakeStatisticsNotion()
    daily = calculate_statistics(statistics_snapshot(), today=date(2026, 2, 15)).daily
    publisher = HeatmapPublisher(fake, "root")
    first = publisher.publish(2026, daily)
    fake.blocks["root"][0]["image"]["caption"] = [
        {
            "type": "text",
            "text": {"content": f"XYZ2NOTION_HEATMAP:2026:{first.content_hash}"},
        }
    ]

    migrated = publisher.publish(2026, daily)
    stable = publisher.publish(2026, daily)

    assert migrated.action == "updated"
    assert stable.action == "unchanged"
    assert fake.uploads == 1
    assert len(fake.blocks["root"]) == 1
    assert fake.blocks["root"][0]["image"]["caption"][0]["text"]["content"] == "\u200b"
