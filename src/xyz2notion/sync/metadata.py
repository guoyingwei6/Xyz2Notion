"""Idempotent Author, Podcast, Episode, and period synchronization."""

from __future__ import annotations

import calendar
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from xyz2notion.models import (
    Author,
    Episode,
    ListeningPeriod,
    ListeningStatus,
    PeriodKind,
    local_date,
)
from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.schema import NotionResource
from xyz2notion.sync.normalizer import MetadataSnapshot
from xyz2notion.sync.notion_table import NotionRowsAPI, NotionTable, UpsertResult

STATUS_NAMES = {
    ListeningStatus.UNPLAYED: "未听",
    ListeningStatus.LISTENING: "在听",
    ListeningStatus.PLAYED: "听过",
}


@dataclass(frozen=True)
class SyncReport:
    """Aggregate row actions for one metadata synchronization."""

    created: int
    updated: int
    unchanged: int
    changed_fields: dict[str, int]


def _text(value: str) -> JsonObject:
    return {"rich_text": rich_text(value)}


def _title(value: str) -> JsonObject:
    return {"title": rich_text(value)}


def _date(value: date | str) -> JsonObject:
    rendered = value.isoformat() if isinstance(value, date) else value
    return {"date": {"start": rendered}}


def _file(url: str | None, name: str) -> JsonObject | None:
    if not url:
        return None
    return {
        "files": [
            {
                "name": name,
                "type": "external",
                "external": {"url": url},
            }
        ]
    }


def _external(url: str | None) -> JsonObject | None:
    if not url:
        return None
    return {"type": "external", "external": {"url": url}}


def _relation(page_ids: list[str]) -> JsonObject:
    return {"relation": [{"id": page_id} for page_id in page_ids]}


def _periods(episodes: tuple[Episode, ...]) -> dict[tuple[PeriodKind, str], ListeningPeriod]:
    result: dict[tuple[PeriodKind, str], ListeningPeriod] = {}
    played_dates: list[date] = []
    for episode in episodes:
        if episode.played_seconds <= 0:
            continue
        played_date = local_date(episode.last_played_at or episode.published_at)
        played_dates.append(played_date)
        year_start = date(played_date.year, 1, 1)
        year_end = date(played_date.year, 12, 31)
        month_start = played_date.replace(day=1)
        month_end = played_date.replace(
            day=calendar.monthrange(played_date.year, played_date.month)[1]
        )
        week_start = played_date - timedelta(days=played_date.weekday())
        week_end = week_start + timedelta(days=6)
        iso_year, iso_week, _ = played_date.isocalendar()
        specs = (
            (PeriodKind.YEAR, f"{played_date.year}", year_start, year_end),
            (
                PeriodKind.MONTH,
                f"{played_date.year}-{played_date.month:02d}",
                month_start,
                month_end,
            ),
            (PeriodKind.WEEK, f"{iso_year}-W{iso_week:02d}", week_start, week_end),
            (PeriodKind.DAY, played_date.isoformat(), played_date, played_date),
        )
        for kind, key, start, end in specs:
            result[(kind, key)] = ListeningPeriod(
                kind=kind,
                key=key,
                start_date=start,
                end_date=end,
            )
    if played_dates:
        result[(PeriodKind.ALL, "all")] = ListeningPeriod(
            kind=PeriodKind.ALL,
            key="all",
            start_date=min(played_dates),
            end_date=max(played_dates),
        )
    return result


def _period_name(period: ListeningPeriod) -> str:
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


class MetadataSynchronizer:
    """Write normalized metadata to managed properties only."""

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
        snapshot: MetadataSnapshot,
        *,
        complete_library_snapshot: bool = False,
    ) -> SyncReport:
        """Upsert a bounded snapshot while preserving unseen and user-managed data."""
        actions: Counter[str] = Counter()
        fields: Counter[str] = Counter()
        author_table = NotionTable(
            self.api,
            self.resources["author"].data_source_id,
            "Author ID",
        )
        podcast_table = NotionTable(
            self.api,
            self.resources["podcast"].data_source_id,
            "PID",
        )
        episode_table = NotionTable(
            self.api,
            self.resources["episode"].data_source_id,
            "EID",
        )

        author_pages: dict[str, str] = {}
        for author in snapshot.authors:
            properties = self._author_properties(author)
            if author_table.property_has_internal_file(author.author_id, "Avatar"):
                properties.pop("Avatar", None)
            result = author_table.upsert(
                author.author_id,
                properties,
                icon=_external(author.avatar_url),
            )
            author_pages[author.author_id] = result.page_id
            self._record(result, actions, fields)

        podcast_pages: dict[str, str] = {}
        for podcast in snapshot.podcasts:
            author_relations = [
                author_pages[author_id]
                for author_id in podcast.author_ids
                if author_id in author_pages
            ]
            properties = {
                "Name": _title(podcast.title),
                "PID": _text(podcast.pid),
                "Description": _text(podcast.description),
                "URL": {"url": f"https://www.xiaoyuzhoufm.com/podcast/{podcast.pid}"},
                "Updated At": _date(podcast.updated_at.isoformat()),
                "Authors": _relation(author_relations),
            }
            cover_file = _file(podcast.image_url, "Podcast cover")
            if cover_file and not podcast_table.property_has_internal_file(
                podcast.pid,
                "Cover",
            ):
                properties["Cover"] = cover_file
            result = podcast_table.upsert(
                podcast.pid,
                properties,
                create_only_properties={
                    "Total Listening Seconds": {"number": 0},
                    "Statistics Baseline Seconds": {"number": 0},
                },
                icon=_external(podcast.image_url),
                cover=_external(podcast.image_url),
            )
            podcast_pages[podcast.pid] = result.page_id
            self._record(result, actions, fields)

        periods = _periods(snapshot.episodes)
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
        period_pages: dict[tuple[PeriodKind, str], str] = {}
        for period_id, period in periods.items():
            result = period_tables[period.kind].upsert(
                period.key,
                {
                    "Name": _title(_period_name(period)),
                    "Period Key": _text(period.key),
                    "Start Date": _date(period.start_date),
                    "End Date": _date(period.end_date),
                },
            )
            period_pages[period_id] = result.page_id
            self._record(result, actions, fields)

        for episode in snapshot.episodes:
            properties = self._episode_properties(
                episode,
                podcast_pages,
                period_pages,
            )
            if episode_table.property_has_internal_file(episode.eid, "Cover"):
                properties.pop("Cover", None)
            result = episode_table.upsert(
                episode.eid,
                properties,
                create_only_properties={
                    "ASR Status": {"select": {"name": "待处理"}},
                    "Statistics Baseline Seconds": {"number": 0},
                    "Statistics Ledger": _text("{}"),
                },
                icon=_external(episode.image_url),
                cover=_external(episode.image_url),
            )
            self._record(result, actions, fields)

        if complete_library_snapshot:
            snapshot_eids = {episode.eid for episode in snapshot.episodes}
            for eid in set(episode_table.keys()) - snapshot_eids:
                stale_properties: dict[str, Any] = {}
                if episode_table.property_value(eid, "In Playlist") == ("checkbox", True):
                    stale_properties["In Playlist"] = {"checkbox": False}
                if episode_table.property_value(eid, "Favorited") == ("checkbox", True):
                    stale_properties["Favorited"] = {"checkbox": False}
                if episode_table.property_value(eid, "Playlist Position") != ("number", None):
                    stale_properties["Playlist Position"] = {"number": None}
                if stale_properties:
                    result = episode_table.upsert(eid, stale_properties)
                    self._record(result, actions, fields)

        return SyncReport(
            created=actions["created"],
            updated=actions["updated"],
            unchanged=actions["unchanged"],
            changed_fields=dict(sorted(fields.items())),
        )

    @staticmethod
    def _author_properties(author: Author) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "Name": _title(author.name),
            "Author ID": _text(author.author_id),
        }
        avatar = _file(author.avatar_url, "Avatar")
        if avatar:
            properties["Avatar"] = avatar
        if author.bio is not None:
            properties["Bio"] = _text(author.bio)
        return properties

    @staticmethod
    def _episode_properties(
        episode: Episode,
        podcast_pages: dict[str, str],
        period_pages: dict[tuple[PeriodKind, str], str],
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "Name": _title(episode.title),
            "EID": _text(episode.eid),
            "Description": _text(episode.description),
            "Published At": _date(episode.published_at.isoformat()),
            "Duration Seconds": {"number": episode.duration_seconds},
            "Played Seconds": {"number": episode.played_seconds},
            "Progress Ring": _text(
                _progress_ring(episode.played_seconds, episode.duration_seconds)
            ),
            "Listening Status": {"select": {"name": STATUS_NAMES[episode.listening_status]}},
            "Liked": {"checkbox": episode.liked},
            "Favorited": {"checkbox": episode.favorited},
            "In Playlist": {"checkbox": episode.in_playlist},
            "Playlist Position": {"number": episode.playlist_position},
        }
        cover = _file(episode.image_url, "Episode cover")
        if cover:
            properties["Cover"] = cover
        if episode.audio_url:
            properties["Audio URL"] = {"url": episode.audio_url}
        if episode.last_played_at:
            properties["Last Played At"] = _date(episode.last_played_at.isoformat())
        podcast_page = podcast_pages.get(episode.pid)
        if podcast_page:
            properties["Podcast"] = _relation([podcast_page])
        if episode.played_seconds > 0:
            played_date = local_date(episode.last_played_at or episode.published_at)
            iso_year, iso_week, _ = played_date.isocalendar()
            relation_keys = {
                "All Period": (PeriodKind.ALL, "all"),
                "Year Period": (PeriodKind.YEAR, f"{played_date.year}"),
                "Month Period": (
                    PeriodKind.MONTH,
                    f"{played_date.year}-{played_date.month:02d}",
                ),
                "Week Period": (PeriodKind.WEEK, f"{iso_year}-W{iso_week:02d}"),
                "Day Period": (PeriodKind.DAY, played_date.isoformat()),
            }
            for property_name, period_id in relation_keys.items():
                page_id = period_pages.get(period_id)
                if page_id:
                    properties[property_name] = _relation([page_id])
        return properties


def _progress_ring(played_seconds: int, duration_seconds: int) -> str:
    percent = round(played_seconds / duration_seconds * 100) if duration_seconds > 0 else 0
    percent = max(0, min(100, percent))
    filled = percent // 10
    return f"{'●' * filled}{'○' * (10 - filled)} {percent}%"
