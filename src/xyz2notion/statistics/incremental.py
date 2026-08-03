"""Failure-safe statistics derived only from Episode rows already stored in Notion."""

from __future__ import annotations

import calendar
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from xyz2notion.models import PeriodKind, local_date, local_today
from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.initializer import HOME_SUMMARY_MARKER_URL
from xyz2notion.notion.schema import NotionResource
from xyz2notion.statistics.calculator import DailyListening
from xyz2notion.statistics.notion_sync import (
    _block_marker_url,
    _block_visible_text,
    _visible_text_with_marker,
)
from xyz2notion.sync.notion_table import NotionRowsAPI, NotionTable

BASELINE_VERSION = "notion-episode-v1"
StatisticsMode = Literal["baseline", "incremental"]


@dataclass(frozen=True)
class IncrementalStatisticsReport:
    """Aggregate, non-identifying result of one Notion-only reconciliation."""

    mode: StatisticsMode
    baseline_episodes: int
    ledger_episodes: int
    delta_seconds: int
    period_rows: int
    podcast_rows: int
    total_seconds: int
    episode_count: int
    played_days: int
    daily: tuple[DailyListening, ...]


@dataclass
class _EpisodeState:
    page_id: str
    eid: str
    podcast_page_id: str | None
    played_seconds: int
    baseline_seconds: int
    baseline_day: date | None
    activity_day: date | None
    ledger: dict[date, int]


def _plain_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    items = value.get("rich_text") or value.get("title")
    if not isinstance(items, list):
        return ""
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in items
        if isinstance(item, Mapping)
    )


def _number(properties: Mapping[str, Any], name: str) -> int:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return 0
    number = value.get("number")
    return max(0, int(number)) if isinstance(number, int | float) else 0


def _day(properties: Mapping[str, Any], name: str) -> date | None:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return None
    date_value = value.get("date")
    if not isinstance(date_value, Mapping):
        return None
    start = date_value.get("start")
    if not isinstance(start, str) or len(start) < 10:
        return None
    try:
        if "T" in start:
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
            return local_date(parsed)
        return date.fromisoformat(start[:10])
    except ValueError:
        return None


def _relation_id(properties: Mapping[str, Any], name: str) -> str | None:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return None
    relations = value.get("relation")
    if not isinstance(relations, list):
        return None
    for relation in relations:
        if isinstance(relation, Mapping) and relation.get("id"):
            return str(relation["id"])
    return None


def _ledger(properties: Mapping[str, Any]) -> dict[date, int]:
    raw = _plain_text(properties.get("Statistics Ledger"))
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Notion Statistics Ledger contains invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("Notion Statistics Ledger must be a date-to-seconds object")
    result: dict[date, int] = {}
    for key, value in decoded.items():
        try:
            day = date.fromisoformat(str(key))
        except ValueError as exc:
            raise ValueError("Notion Statistics Ledger contains an invalid date") from exc
        if not isinstance(value, int | float) or value < 0:
            raise ValueError("Notion Statistics Ledger contains invalid seconds")
        if value > 0:
            result[day] = int(value)
    return result


def _ledger_text(values: Mapping[date, int]) -> JsonObject:
    payload = {day.isoformat(): values[day] for day in sorted(values) if values[day] > 0}
    return {"rich_text": rich_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))}


def _date_value(value: date) -> JsonObject:
    return {"date": {"start": value.isoformat()}}


def _text(value: str) -> JsonObject:
    return {"rich_text": rich_text(value)}


def _title(value: str) -> JsonObject:
    return {"title": rich_text(value)}


def _period_key(kind: PeriodKind, day: date) -> str:
    if kind is PeriodKind.YEAR:
        return str(day.year)
    if kind is PeriodKind.MONTH:
        return f"{day.year}-{day.month:02d}"
    if kind is PeriodKind.WEEK:
        iso_year, iso_week, _ = day.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if kind is PeriodKind.DAY:
        return day.isoformat()
    return "all"


def _period_name(kind: PeriodKind, key: str) -> str:
    if kind is PeriodKind.ALL:
        return "全部"
    if kind is PeriodKind.YEAR:
        return f"{key} 年"
    if kind is PeriodKind.MONTH:
        year, month = key.split("-")
        return f"{year} 年 {int(month)} 月"
    if kind is PeriodKind.WEEK:
        return f"{key} 周"
    return key


def _period_bounds(kind: PeriodKind, key: str, fallback: date) -> tuple[date, date]:
    if kind is PeriodKind.ALL:
        return fallback, fallback
    if kind is PeriodKind.YEAR:
        year = int(key)
        return date(year, 1, 1), date(year, 12, 31)
    if kind is PeriodKind.MONTH:
        year, month = (int(part) for part in key.split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if kind is PeriodKind.WEEK:
        iso_year, iso_week = key.split("-W")
        start = date.fromisocalendar(int(iso_year), int(iso_week), 1)
        return start, start + timedelta(days=6)
    day = date.fromisoformat(key)
    return day, day


def _page_map(pages: list[JsonObject], key_property: str) -> dict[str, JsonObject]:
    result: dict[str, JsonObject] = {}
    for page in pages:
        properties = page.get("properties")
        if not isinstance(properties, Mapping):
            continue
        key = _plain_text(properties.get(key_property))
        if key:
            result[key] = page
    return result


def _page_properties(page: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = page.get("properties")
    return properties if isinstance(properties, Mapping) else {}


class NotionIncrementalStatistics:
    """Baseline once, then reconcile statistics from an Episode-local daily ledger."""

    def __init__(
        self,
        api: NotionRowsAPI,
        resources: dict[str, NotionResource],
        *,
        root_page_id: str | None = None,
    ) -> None:
        self.api = api
        self.resources = resources
        self.root_page_id = root_page_id

    def _episodes(self) -> list[_EpisodeState]:
        pages = self.api.query_data_source(
            self.resources["episode"].data_source_id,
            {"page_size": 100},
        )
        result: list[_EpisodeState] = []
        for page in pages:
            properties = _page_properties(page)
            eid = _plain_text(properties.get("EID"))
            if not eid or not page.get("id"):
                continue
            result.append(
                _EpisodeState(
                    page_id=str(page["id"]),
                    eid=eid,
                    podcast_page_id=_relation_id(properties, "Podcast"),
                    played_seconds=_number(properties, "Played Seconds"),
                    baseline_seconds=_number(properties, "Statistics Baseline Seconds"),
                    baseline_day=_day(properties, "Statistics Baseline Date"),
                    activity_day=(
                        _day(properties, "Last Played At") or _day(properties, "Published At")
                    ),
                    ledger=_ledger(properties),
                )
            )
        return result

    def _period_pages(self) -> dict[PeriodKind, dict[str, JsonObject]]:
        return {
            kind: _page_map(
                self.api.query_data_source(
                    self.resources[kind.value].data_source_id,
                    {"page_size": 100},
                ),
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

    def _podcast_pages(self) -> dict[str, JsonObject]:
        pages = self.api.query_data_source(
            self.resources["podcast"].data_source_id,
            {"page_size": 100},
        )
        return {
            str(page["id"]): page
            for page in pages
            if page.get("id") and _plain_text(_page_properties(page).get("PID"))
        }

    def _is_initialized(self, period_pages: dict[PeriodKind, dict[str, JsonObject]]) -> bool:
        total = period_pages[PeriodKind.ALL].get("all")
        if total is None:
            return False
        return (
            _plain_text(_page_properties(total).get("Statistics Baseline Version"))
            == BASELINE_VERSION
        )

    def _establish_baseline(
        self,
        episodes: list[_EpisodeState],
        period_pages: dict[PeriodKind, dict[str, JsonObject]],
        podcast_pages: dict[str, JsonObject],
        *,
        today: date,
    ) -> IncrementalStatisticsReport:
        total = period_pages[PeriodKind.ALL].get("all")
        if total is None:
            raise ValueError("Notion statistics baseline requires the existing all-period row")
        baseline_episodes = 0
        for episode in episodes:
            if episode.played_seconds <= 0:
                continue
            baseline_day = episode.baseline_day or episode.activity_day or today
            self.api.update_page(
                episode.page_id,
                {
                    "properties": {
                        "Statistics Baseline Seconds": {"number": episode.played_seconds},
                        "Statistics Baseline Date": _date_value(baseline_day),
                        "Statistics Ledger": _ledger_text({}),
                    }
                },
            )
            episode.baseline_seconds = episode.played_seconds
            episode.baseline_day = baseline_day
            episode.ledger = {}
            baseline_episodes += 1

        period_rows = 0
        for pages in period_pages.values():
            for page in pages.values():
                properties = _page_properties(page)
                self.api.update_page(
                    str(page["id"]),
                    {
                        "properties": {
                            "Statistics Baseline Seconds": {
                                "number": _number(properties, "Exact Listening Seconds")
                            },
                            "Statistics Baseline Podcast Count": {
                                "number": _number(properties, "Podcast Count")
                            },
                            "Statistics Baseline Played Days": {
                                "number": _number(properties, "Played Days")
                            },
                        }
                    },
                )
                period_rows += 1

        for page in podcast_pages.values():
            properties = _page_properties(page)
            self.api.update_page(
                str(page["id"]),
                {
                    "properties": {
                        "Statistics Baseline Seconds": {
                            "number": _number(properties, "Total Listening Seconds")
                        }
                    }
                },
            )

        self.api.update_page(
            str(total["id"]),
            {
                "properties": {
                    "Statistics Baseline Version": _text(BASELINE_VERSION),
                }
            },
        )
        total_properties = _page_properties(total)
        return IncrementalStatisticsReport(
            mode="baseline",
            baseline_episodes=baseline_episodes,
            ledger_episodes=0,
            delta_seconds=0,
            period_rows=period_rows,
            podcast_rows=len(podcast_pages),
            total_seconds=_number(total_properties, "Exact Listening Seconds"),
            episode_count=sum(episode.played_seconds > 0 for episode in episodes),
            played_days=_number(total_properties, "Played Days"),
            daily=(),
        )

    def _capture_deltas(self, episodes: list[_EpisodeState], *, today: date) -> tuple[int, int]:
        ledger_episodes = 0
        delta_seconds = 0
        for episode in episodes:
            counted = episode.baseline_seconds + sum(episode.ledger.values())
            delta = max(0, episode.played_seconds - counted)
            if delta <= 0:
                continue
            ledger_day = episode.activity_day or episode.baseline_day or today
            episode.ledger[ledger_day] = episode.ledger.get(ledger_day, 0) + delta
            self.api.update_page(
                episode.page_id,
                {"properties": {"Statistics Ledger": _ledger_text(episode.ledger)}},
            )
            ledger_episodes += 1
            delta_seconds += delta
        return ledger_episodes, delta_seconds

    def _update_home_summary(
        self,
        *,
        total_seconds: int,
        episode_count: int,
        played_days: int,
    ) -> None:
        if self.root_page_id is None:
            return
        list_children = getattr(self.api, "list_block_children", None)
        update_block = getattr(self.api, "update_block", None)
        if not callable(list_children) or not callable(update_block):
            return
        hours_text = f"{total_seconds / 3600:.1f}".rstrip("0").rstrip(".")
        summary = f"🎧 累计收听 {hours_text} 小时 · {episode_count} 期 · {played_days} 天"
        blocks = list_children(self.root_page_id)
        managed = next(
            (block for block in blocks if _block_marker_url(block) == HOME_SUMMARY_MARKER_URL),
            None,
        )
        if managed is None or _block_visible_text(managed) == summary:
            return
        update_block(
            str(managed["id"]),
            {
                "paragraph": {
                    "rich_text": _visible_text_with_marker(summary, HOME_SUMMARY_MARKER_URL)
                }
            },
        )

    def _reconcile(
        self,
        episodes: list[_EpisodeState],
        period_pages: dict[PeriodKind, dict[str, JsonObject]],
        podcast_pages: dict[str, JsonObject],
        *,
        today: date,
        ledger_episodes: int,
        delta_seconds: int,
    ) -> IncrementalStatisticsReport:
        additions: dict[PeriodKind, dict[str, int]] = {
            kind: defaultdict(int)
            for kind in (
                PeriodKind.ALL,
                PeriodKind.YEAR,
                PeriodKind.MONTH,
                PeriodKind.WEEK,
                PeriodKind.DAY,
            )
        }
        period_episode_ids: dict[PeriodKind, dict[str, set[str]]] = {
            kind: defaultdict(set) for kind in additions
        }
        period_podcast_ids: dict[PeriodKind, dict[str, set[str]]] = {
            kind: defaultdict(set) for kind in additions
        }
        period_days: dict[PeriodKind, dict[str, set[date]]] = {
            kind: defaultdict(set) for kind in additions
        }
        podcast_additions: dict[str, int] = defaultdict(int)

        for episode in episodes:
            signals: dict[date, int] = {}
            if episode.baseline_seconds > 0 and episode.baseline_day is not None:
                signals[episode.baseline_day] = episode.baseline_seconds
            for day, seconds in episode.ledger.items():
                signals[day] = signals.get(day, 0) + seconds
                if episode.podcast_page_id:
                    podcast_additions[episode.podcast_page_id] += seconds
            for day in signals:
                for kind in additions:
                    key = _period_key(kind, day)
                    period_episode_ids[kind][key].add(episode.eid)
                    period_days[kind][key].add(day)
                    if episode.podcast_page_id:
                        period_podcast_ids[kind][key].add(episode.podcast_page_id)
            for day, seconds in episode.ledger.items():
                for kind in additions:
                    additions[kind][_period_key(kind, day)] += seconds

        period_rows = 0
        daily_values: list[tuple[date, int, int, int]] = []
        total_seconds = 0
        total_episode_count = 0
        total_played_days = 0
        for kind, pages in period_pages.items():
            table = NotionTable(
                self.api,
                self.resources[kind.value].data_source_id,
                "Period Key",
            )
            keys = (
                set(pages)
                | set(additions[kind])
                | set(period_episode_ids[kind])
                | ({"all"} if kind is PeriodKind.ALL else set())
            )
            for key in sorted(keys):
                page = pages.get(key)
                properties = _page_properties(page or {})
                baseline_seconds = _number(properties, "Statistics Baseline Seconds")
                exact_seconds = baseline_seconds + additions[kind].get(key, 0)
                baseline_podcasts = _number(properties, "Statistics Baseline Podcast Count")
                baseline_days = _number(properties, "Statistics Baseline Played Days")
                podcast_count = max(
                    baseline_podcasts,
                    len(period_podcast_ids[kind].get(key, set())),
                )
                played_days = max(
                    baseline_days,
                    len(period_days[kind].get(key, set())),
                )
                episode_count = len(period_episode_ids[kind].get(key, set()))
                if kind is PeriodKind.ALL:
                    signal_days = period_days[kind].get(key, set())
                    existing_start = _day(properties, "Start Date")
                    existing_end = _day(properties, "End Date")
                    starts = [value for value in (existing_start, *signal_days) if value]
                    ends = [value for value in (existing_end, *signal_days) if value]
                    start = min(starts, default=today)
                    end = max(ends, default=today)
                else:
                    start, end = _period_bounds(kind, key, today)
                table.upsert(
                    key,
                    {
                        "Name": _title(_period_name(kind, key)),
                        "Period Key": _text(key),
                        "Start Date": _date_value(start),
                        "End Date": _date_value(end),
                        "Exact Listening Seconds": {"number": exact_seconds},
                        "收听小时": {"number": round(exact_seconds / 3600, 1)},
                        "Podcast Count": {"number": podcast_count},
                        "Played Days": {"number": played_days},
                        "Statistics Source": _text("notion_incremental"),
                        "Statistics Baseline Seconds": {"number": baseline_seconds},
                        "Statistics Baseline Podcast Count": {"number": baseline_podcasts},
                        "Statistics Baseline Played Days": {"number": baseline_days},
                    },
                )
                period_rows += 1
                if kind is PeriodKind.ALL and key == "all":
                    total_seconds = exact_seconds
                    total_episode_count = episode_count
                    total_played_days = played_days
                if kind is PeriodKind.DAY:
                    daily_values.append(
                        (
                            date.fromisoformat(key),
                            exact_seconds,
                            episode_count,
                            podcast_count,
                        )
                    )

        totals_by_podcast: dict[str, int] = {}
        for page_id, page in podcast_pages.items():
            properties = _page_properties(page)
            totals_by_podcast[page_id] = _number(
                properties,
                "Statistics Baseline Seconds",
            ) + podcast_additions.get(page_id, 0)
        ranked = sorted(
            totals_by_podcast.items(),
            key=lambda item: (
                -item[1],
                _plain_text(_page_properties(podcast_pages[item[0]]).get("PID")),
            ),
        )
        podcast_table = NotionTable(
            self.api,
            self.resources["podcast"].data_source_id,
            "PID",
        )
        for rank, (page_id, total) in enumerate(ranked, start=1):
            page = podcast_pages[page_id]
            properties = _page_properties(page)
            pid = _plain_text(properties.get("PID"))
            podcast_table.upsert(
                pid,
                {
                    "Total Listening Seconds": {"number": total},
                    "Rank": {"number": rank},
                    "Statistics Baseline Seconds": {
                        "number": _number(properties, "Statistics Baseline Seconds")
                    },
                },
            )

        maximum = max((seconds for _, seconds, _, _ in daily_values), default=0)
        daily = tuple(
            DailyListening(
                day=day,
                listening_seconds=seconds,
                episode_count=episode_count,
                podcast_count=podcast_count,
                level=(
                    0
                    if seconds <= 0 or maximum <= 0
                    else min(4, max(1, (seconds * 4 + maximum - 1) // maximum))
                ),
            )
            for day, seconds, episode_count, podcast_count in sorted(daily_values)
        )
        self._update_home_summary(
            total_seconds=total_seconds,
            episode_count=total_episode_count,
            played_days=total_played_days,
        )
        return IncrementalStatisticsReport(
            mode="incremental",
            baseline_episodes=0,
            ledger_episodes=ledger_episodes,
            delta_seconds=delta_seconds,
            period_rows=period_rows,
            podcast_rows=len(podcast_pages),
            total_seconds=total_seconds,
            episode_count=total_episode_count,
            played_days=total_played_days,
            daily=daily,
        )

    def sync(self, *, today: date | None = None) -> IncrementalStatisticsReport:
        """Create the one-time baseline or deterministically apply new Episode deltas."""
        current_day = today or local_today()
        episodes = self._episodes()
        period_pages = self._period_pages()
        podcast_pages = self._podcast_pages()
        if not self._is_initialized(period_pages):
            return self._establish_baseline(
                episodes,
                period_pages,
                podcast_pages,
                today=current_day,
            )
        ledger_episodes, delta_seconds = self._capture_deltas(episodes, today=current_day)
        return self._reconcile(
            episodes,
            period_pages,
            podcast_pages,
            today=current_day,
            ledger_episodes=ledger_episodes,
            delta_seconds=delta_seconds,
        )
