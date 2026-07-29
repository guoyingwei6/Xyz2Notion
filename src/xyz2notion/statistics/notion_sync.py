"""Write exact statistics, rankings, and one managed heatmap block to Notion."""

from __future__ import annotations

import calendar
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from xyz2notion.models import PeriodKind
from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.schema import NotionResource
from xyz2notion.statistics.calculator import (
    DailyListening,
    PeriodStatistics,
    StatisticsSnapshot,
)
from xyz2notion.statistics.heatmap import render_heatmap_png
from xyz2notion.sync.notion_table import NotionRowsAPI, NotionTable, UpsertResult

HEATMAP_MARKER = "XYZ2NOTION_HEATMAP"


class HeatmapNotionAPI(Protocol):
    """File and block methods required by the heatmap publisher."""

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

    def upload_file(
        self,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> str: ...


@dataclass(frozen=True)
class StatisticsSyncReport:
    """Row-level outcomes for exact statistics and ranks."""

    created: int
    updated: int
    unchanged: int
    changed_fields: dict[str, int]


@dataclass(frozen=True)
class HeatmapPublishResult:
    """Idempotent heatmap publication outcome."""

    action: str
    content_hash: str
    block_id: str | None


def _title(value: str) -> JsonObject:
    return {"title": rich_text(value)}


def _text(value: str) -> JsonObject:
    return {"rich_text": rich_text(value)}


def _date_value(value: date) -> JsonObject:
    return {"date": {"start": value.isoformat()}}


def _period_name(period: PeriodStatistics) -> str:
    if period.kind is PeriodKind.ALL:
        return "全部"
    if period.kind is PeriodKind.YEAR:
        return f"{period.key} 年"
    if period.kind is PeriodKind.MONTH:
        year, month = period.key.split("-")
        return f"{year} 年 {int(month)} 月"
    if period.kind is PeriodKind.WEEK:
        return f"{period.key} 周"
    return period.key


def _period_bounds(
    period: PeriodStatistics,
    *,
    fallback_day: date,
    daily: tuple[DailyListening, ...],
) -> tuple[date, date]:
    if period.kind is PeriodKind.ALL:
        days = [value.day for value in daily]
        return (min(days), max(days)) if days else (fallback_day, fallback_day)
    if period.kind is PeriodKind.YEAR:
        year_number = int(period.key)
        return date(year_number, 1, 1), date(year_number, 12, 31)
    if period.kind is PeriodKind.MONTH:
        year_number, month_number = (int(value) for value in period.key.split("-"))
        return (
            date(year_number, month_number, 1),
            date(
                year_number,
                month_number,
                calendar.monthrange(year_number, month_number)[1],
            ),
        )
    if period.kind is PeriodKind.WEEK:
        iso_year, iso_week = period.key.split("-W")
        start = date.fromisocalendar(int(iso_year), int(iso_week), 1)
        return start, start + timedelta(days=6)
    day = date.fromisoformat(period.key)
    return day, day


class StatisticsSynchronizer:
    """Upsert exact statistics and podcast rank without touching other fields."""

    def __init__(
        self,
        api: NotionRowsAPI,
        resources: dict[str, NotionResource],
    ) -> None:
        self.api = api
        self.resources = resources

    @staticmethod
    def _record(result: UpsertResult, actions: Counter[str], fields: Counter[str]) -> None:
        actions[result.action] += 1
        fields.update(result.changed_properties)

    def sync(
        self,
        statistics: StatisticsSnapshot,
        *,
        today: date | None = None,
    ) -> StatisticsSyncReport:
        current_day = today or date.today()
        actions: Counter[str] = Counter()
        fields: Counter[str] = Counter()
        period_tables = {
            kind: NotionTable(
                self.api,
                self.resources[kind.value].data_source_id,
                "Period Key",
            )
            for kind in (
                PeriodKind.ALL,
                PeriodKind.YEAR,
                PeriodKind.MONTH,
                PeriodKind.WEEK,
                PeriodKind.DAY,
            )
        }
        periods = (
            statistics.total,
            *statistics.years,
            *statistics.months,
            *statistics.weeks,
            *statistics.days,
        )
        for period in periods:
            start, end = _period_bounds(
                period,
                fallback_day=current_day,
                daily=statistics.daily,
            )
            result = period_tables[period.kind].upsert(
                period.key,
                {
                    "Name": _title(_period_name(period)),
                    "Period Key": _text(period.key),
                    "Start Date": _date_value(start),
                    "End Date": _date_value(end),
                    "Exact Listening Seconds": {"number": period.listening_seconds},
                    "Exact Listening Hours": {"number": round(period.listening_seconds / 3600, 3)},
                    "Podcast Count": {"number": period.podcast_count},
                    "Played Days": {"number": period.played_days},
                    "Statistics Source": _text(period.source),
                },
            )
            self._record(result, actions, fields)

        podcast_table = NotionTable(
            self.api,
            self.resources["podcast"].data_source_id,
            "PID",
        )
        for ranking in statistics.ranking:
            result = podcast_table.upsert(
                ranking.pid,
                {
                    "Name": _title(ranking.title),
                    "PID": _text(ranking.pid),
                    "Rank": {"number": ranking.rank},
                    "Total Listening Seconds": {"number": ranking.listening_seconds},
                },
            )
            self._record(result, actions, fields)
        return StatisticsSyncReport(
            created=actions["created"],
            updated=actions["updated"],
            unchanged=actions["unchanged"],
            changed_fields=dict(sorted(fields.items())),
        )


def _caption_text(block: Mapping[str, Any]) -> str:
    image = block.get("image")
    if not isinstance(image, Mapping):
        return ""
    caption = image.get("caption")
    if not isinstance(caption, list):
        return ""
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in caption
        if isinstance(item, Mapping)
    )


class HeatmapPublisher:
    """Upload and reconcile exactly one image block per calendar year."""

    def __init__(self, api: HeatmapNotionAPI, root_page_id: str) -> None:
        self.api = api
        self.root_page_id = root_page_id

    def publish(
        self,
        year: int,
        daily: tuple[DailyListening, ...],
    ) -> HeatmapPublishResult:
        png = render_heatmap_png(year, daily)
        content_hash = hashlib.sha256(png).hexdigest()[:16]
        marker_prefix = f"{HEATMAP_MARKER}:{year}:"
        marker = f"{marker_prefix}{content_hash}"
        managed_block: JsonObject | None = None
        for block in self.api.list_block_children(self.root_page_id):
            if block.get("type") == "image" and _caption_text(block).startswith(marker_prefix):
                managed_block = block
                break
        if managed_block is not None and _caption_text(managed_block) == marker:
            return HeatmapPublishResult(
                action="unchanged",
                content_hash=content_hash,
                block_id=str(managed_block.get("id") or "") or None,
            )

        upload_id = self.api.upload_file(
            f"xyz2notion-heatmap-{year}-{content_hash}.png",
            "image/png",
            png,
        )
        image = {
            "type": "file_upload",
            "file_upload": {"id": upload_id},
            "caption": rich_text(marker),
        }
        if managed_block is not None and managed_block.get("id"):
            block_id = str(managed_block["id"])
            self.api.update_block(block_id, {"image": image})
            return HeatmapPublishResult(
                action="updated",
                content_hash=content_hash,
                block_id=block_id,
            )
        created = self.api.append_block_children(
            self.root_page_id,
            [
                {
                    "object": "block",
                    "type": "image",
                    "image": image,
                }
            ],
        )
        block_id = str(created[0].get("id") or "") if created else ""
        return HeatmapPublishResult(
            action="created",
            content_hash=content_hash,
            block_id=block_id or None,
        )
