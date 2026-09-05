"""Write exact statistics, rankings, and one managed heatmap block to Notion."""

from __future__ import annotations

import calendar
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from xyz2notion.models import PeriodKind, local_today
from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.initializer import HOME_SUMMARY_MARKER_URL
from xyz2notion.notion.schema import NotionResource
from xyz2notion.statistics.calculator import (
    DailyListening,
    PeriodStatistics,
    StatisticsSnapshot,
)
from xyz2notion.statistics.heatmap import render_heatmap_png
from xyz2notion.sync.notion_table import NotionRowsAPI, NotionTable, UpsertResult

HEATMAP_MARKER = "XYZ2NOTION_HEATMAP"
HEATMAP_MARKER_URL_PREFIX = "https://xyz2notion.local/heatmap/"


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
        root_page_id: str | None = None,
    ) -> None:
        self.api = api
        self.resources = resources
        self.root_page_id = root_page_id

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
        current_day = today or local_today()
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
                    "收听小时": {"number": round(period.listening_seconds / 3600, 1)},
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
        self._update_home_summary(statistics)
        return StatisticsSyncReport(
            created=actions["created"],
            updated=actions["updated"],
            unchanged=actions["unchanged"],
            changed_fields=dict(sorted(fields.items())),
        )

    def _update_home_summary(self, statistics: StatisticsSnapshot) -> None:
        """Refresh the compact V3 summary when block APIs and a root page are available."""
        if self.root_page_id is None:
            return
        list_children = getattr(self.api, "list_block_children", None)
        update_block = getattr(self.api, "update_block", None)
        if not callable(list_children) or not callable(update_block):
            return
        total = statistics.total
        hours = total.listening_seconds / 3600
        hours_text = f"{hours:.1f}".rstrip("0").rstrip(".")
        summary = (
            f"🎧 累计收听 {hours_text} 小时 · {total.episode_count} 期 · {total.played_days} 天"
        )
        blocks = list_children(self.root_page_id)
        managed_block: Mapping[str, Any] | None = None
        for block in blocks:
            if _block_marker_url(block) != HOME_SUMMARY_MARKER_URL:
                continue
            managed_block = block
            break
        if managed_block is None:
            placeholders = [
                block
                for block in blocks
                if block.get("type") == "paragraph"
                and _block_visible_text(block) == "🎧 收听数据将在首次统计同步后显示。"
            ]
            if len(placeholders) == 1:
                managed_block = placeholders[0]
        if managed_block is not None:
            block = managed_block
            if _block_visible_text(block) == summary:
                return
            update_block(
                str(block["id"]),
                {
                    "paragraph": {
                        "rich_text": _visible_text_with_marker(
                            summary,
                            HOME_SUMMARY_MARKER_URL,
                        )
                    }
                },
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


def _rich_text_items(block: Mapping[str, Any]) -> Sequence[object]:
    body = block.get(str(block.get("type")))
    if not isinstance(body, Mapping):
        return ()
    items = body.get("caption") if block.get("type") == "image" else body.get("rich_text")
    return items if isinstance(items, Sequence) else ()


def _text_url(item: Mapping[str, Any]) -> str:
    href = item.get("href")
    if href:
        return str(href)
    text = item.get("text")
    link = text.get("link") if isinstance(text, Mapping) else None
    return str(link.get("url") or "") if isinstance(link, Mapping) else ""


def _block_marker_url(block: Mapping[str, Any]) -> str:
    for item in _rich_text_items(block):
        if isinstance(item, Mapping):
            url = _text_url(item)
            if url:
                return url
    return ""


def _block_visible_text(block: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for item in _rich_text_items(block):
        if not isinstance(item, Mapping) or _text_url(item):
            continue
        parts.append(str(item.get("plain_text") or item.get("text", {}).get("content") or ""))
    return "".join(parts)


def _visible_text_with_marker(text: str, marker_url: str) -> list[JsonObject]:
    return [
        *rich_text(text),
        {
            "type": "text",
            "text": {
                "content": "\u200b",
                "link": {"url": marker_url},
            },
        },
    ]


def _heatmap_marker_url(year: int, content_hash: str) -> str:
    return f"{HEATMAP_MARKER_URL_PREFIX}{year}/{content_hash}"


def _heatmap_hash_from_url(url: str, year: int) -> str:
    prefix = f"{HEATMAP_MARKER_URL_PREFIX}{year}/"
    return url.removeprefix(prefix) if url.startswith(prefix) else ""


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
        managed_marker: JsonObject | None = None
        managed_images: list[JsonObject] = []
        target_parent = self.root_page_id
        visited: set[str] = set()

        def walk(parent_id: str) -> None:
            nonlocal managed_block, managed_marker, target_parent
            if parent_id in visited:
                return
            visited.add(parent_id)
            blocks = self.api.list_block_children(parent_id)
            is_record_column = any(
                block.get("type") == "heading_2" and _block_visible_text(block) == "播客记录"
                for block in blocks
            )
            if is_record_column:
                target_parent = parent_id
            for block in blocks:
                caption = _caption_text(block)
                marker_url = _block_marker_url(block)
                owned_image = block.get("type") == "image" and (
                    caption.startswith(marker_prefix)
                    or bool(_heatmap_hash_from_url(marker_url, year))
                )
                if owned_image:
                    managed_images.append(block)
                if (
                    managed_block is None
                    and block.get("type") == "image"
                    and caption.startswith(marker_prefix)
                ):
                    managed_block = block
                if (
                    owned_image
                    and managed_marker is None
                    and _heatmap_hash_from_url(marker_url, year)
                ):
                    managed_marker = block
                    managed_block = block
                child_id = block.get("id")
                if block.get("has_children") and child_id:
                    walk(str(child_id))

        walk(self.root_page_id)
        self._archive_duplicate_images(
            keep_block_id=str(managed_block.get("id") or "") if managed_block else "",
            candidates=managed_images,
        )
        marker_hash = (
            _heatmap_hash_from_url(_block_marker_url(managed_marker), year)
            if managed_marker is not None
            else ""
        )
        if managed_block is not None and marker_hash == content_hash:
            return HeatmapPublishResult(
                action="unchanged",
                content_hash=content_hash,
                block_id=str(managed_block.get("id") or "") or None,
            )
        if managed_block is not None and _caption_text(managed_block) == marker:
            existing_image = managed_block.get("image")
            if isinstance(existing_image, Mapping) and managed_block.get("id"):
                block_id = str(managed_block["id"])
                self.api.update_block(
                    block_id,
                    {
                        "image": {
                            **dict(existing_image),
                            "caption": _visible_text_with_marker(
                                "",
                                _heatmap_marker_url(year, content_hash),
                            ),
                        }
                    },
                )
                return HeatmapPublishResult(
                    action="updated",
                    content_hash=content_hash,
                    block_id=block_id,
                )

        upload_id = self.api.upload_file(
            f"xyz2notion-heatmap-{year}-{content_hash}.png",
            "image/png",
            png,
        )
        marker_url = _heatmap_marker_url(year, content_hash)
        image = {
            "type": "file_upload",
            "file_upload": {"id": upload_id},
            "caption": _visible_text_with_marker("", marker_url),
        }
        if managed_block is not None and managed_block.get("id"):
            block_id = str(managed_block["id"])
            image_update = dict(image)
            image_update.pop("type", None)
            self.api.update_block(block_id, {"image": image_update})
            return HeatmapPublishResult(
                action="updated",
                content_hash=content_hash,
                block_id=block_id,
            )
        created = self.api.append_block_children(
            target_parent,
            [
                {
                    "object": "block",
                    "type": "image",
                    "image": image,
                },
            ],
        )
        image_block = next(
            (block for block in created if block.get("type") == "image"),
            None,
        )
        block_id = str(image_block.get("id") or "") if image_block else ""
        return HeatmapPublishResult(
            action="created",
            content_hash=content_hash,
            block_id=block_id or None,
        )

    def _archive_duplicate_images(
        self,
        *,
        keep_block_id: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        if not keep_block_id or len(candidates) <= 1:
            return
        delete_block = getattr(self.api, "delete_block", None)
        if not callable(delete_block):
            return
        seen: set[str] = set()
        for block in candidates:
            block_id = str(block.get("id") or "")
            if not block_id or block_id == keep_block_id or block_id in seen:
                continue
            seen.add(block_id)
            delete_block(block_id)
