"""Deterministic listening statistics with explicit source provenance."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal

from xyz2notion.models import Episode, PeriodKind
from xyz2notion.sync.normalizer import MetadataSnapshot

StatisticsSource = Literal["mileage", "episodes", "monthly_wrapped", "mixed"]


@dataclass(frozen=True)
class MonthlyWrappedValue:
    """Official historical monthly totals returned by Xiaoyuzhou."""

    year: int
    month: int
    listening_seconds: int
    played_days: int

    @property
    def key(self) -> str:
        return f"{self.year}-{self.month:02d}"


@dataclass(frozen=True)
class PeriodStatistics:
    """Counts and listening duration for one calendar period."""

    kind: PeriodKind
    key: str
    podcast_count: int
    episode_count: int
    played_days: int
    listening_seconds: int
    source: StatisticsSource


@dataclass(frozen=True)
class PodcastRanking:
    """One exact mileage-based podcast rank."""

    rank: int
    pid: str
    title: str
    listening_seconds: int


@dataclass(frozen=True)
class DailyListening:
    """Episode-derived daily listening approximation for heatmaps."""

    day: date
    listening_seconds: int
    episode_count: int
    podcast_count: int
    level: int


@dataclass(frozen=True)
class StatisticsSnapshot:
    """Complete statistics result used by Notion and heatmap rendering."""

    total: PeriodStatistics
    years: tuple[PeriodStatistics, ...]
    months: tuple[PeriodStatistics, ...]
    weeks: tuple[PeriodStatistics, ...]
    days: tuple[PeriodStatistics, ...]
    ranking: tuple[PodcastRanking, ...]
    daily: tuple[DailyListening, ...]


def _played_episode_day(episode: Episode) -> date | None:
    if episode.played_seconds <= 0:
        return None
    return (episode.last_played_at or episode.published_at).date()


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


def _episode_periods(
    episodes: tuple[Episode, ...],
    kind: PeriodKind,
) -> dict[str, PeriodStatistics]:
    grouped: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        played_day = _played_episode_day(episode)
        if played_day is not None:
            grouped[_period_key(kind, played_day)].append(episode)
    result: dict[str, PeriodStatistics] = {}
    for key, values in grouped.items():
        played_days = {
            played_day
            for episode in values
            if (played_day := _played_episode_day(episode)) is not None
        }
        result[key] = PeriodStatistics(
            kind=kind,
            key=key,
            podcast_count=len({episode.pid for episode in values}),
            episode_count=len({episode.eid for episode in values}),
            played_days=len(played_days),
            listening_seconds=sum(episode.played_seconds for episode in values),
            source="episodes",
        )
    return result


def _heat_levels(day_seconds: dict[date, int]) -> dict[date, int]:
    maximum = max(day_seconds.values(), default=0)
    if maximum <= 0:
        return {day: 0 for day in day_seconds}
    return {
        day: 0 if seconds <= 0 else min(4, max(1, (seconds * 4 + maximum - 1) // maximum))
        for day, seconds in day_seconds.items()
    }


def calculate_statistics(
    snapshot: MetadataSnapshot,
    monthly_wrapped: tuple[MonthlyWrappedValue, ...] = (),
    *,
    today: date | None = None,
) -> StatisticsSnapshot:
    """Calculate exact mileage totals and explicitly sourced period statistics."""
    current_day = today or date.today()
    day_stats = _episode_periods(snapshot.episodes, PeriodKind.DAY)
    week_stats = _episode_periods(snapshot.episodes, PeriodKind.WEEK)
    month_stats = _episode_periods(snapshot.episodes, PeriodKind.MONTH)

    wrapped_by_key = {value.key: value for value in monthly_wrapped}
    for key, wrapped in wrapped_by_key.items():
        year_number, month_number = (int(part) for part in key.split("-"))
        if (year_number, month_number) >= (current_day.year, current_day.month):
            continue
        existing = month_stats.get(key)
        month_stats[key] = PeriodStatistics(
            kind=PeriodKind.MONTH,
            key=key,
            podcast_count=existing.podcast_count if existing else 0,
            episode_count=existing.episode_count if existing else 0,
            played_days=wrapped.played_days,
            listening_seconds=wrapped.listening_seconds,
            source="monthly_wrapped",
        )

    year_episode_stats = _episode_periods(snapshot.episodes, PeriodKind.YEAR)
    months_by_year: dict[str, list[PeriodStatistics]] = defaultdict(list)
    for month in month_stats.values():
        months_by_year[month.key[:4]].append(month)
    year_stats: dict[str, PeriodStatistics] = {}
    for key in set(year_episode_stats) | set(months_by_year):
        episode_value = year_episode_stats.get(key)
        months = months_by_year.get(key, [])
        sources = {month.source for month in months}
        source: StatisticsSource = next(iter(sources)) if len(sources) == 1 else "mixed"
        year_stats[key] = PeriodStatistics(
            kind=PeriodKind.YEAR,
            key=key,
            podcast_count=episode_value.podcast_count if episode_value else 0,
            episode_count=episode_value.episode_count if episode_value else 0,
            played_days=sum(month.played_days for month in months),
            listening_seconds=sum(month.listening_seconds for month in months),
            source=source,
        )

    played_pids = {episode.pid for episode in snapshot.episodes if episode.played_seconds > 0}
    listened_podcasts = tuple(
        podcast
        for podcast in snapshot.podcasts
        if podcast.pid in played_pids and podcast.total_listening_seconds > 0
    )
    total_seconds = sum(podcast.total_listening_seconds for podcast in listened_podcasts)
    played_days = len(day_stats)
    total = PeriodStatistics(
        kind=PeriodKind.ALL,
        key="all",
        podcast_count=len(played_pids),
        episode_count=len(
            {episode.eid for episode in snapshot.episodes if episode.played_seconds > 0}
        ),
        played_days=played_days,
        listening_seconds=total_seconds,
        source="mileage",
    )
    ordered_podcasts = sorted(
        listened_podcasts,
        key=lambda podcast: (-podcast.total_listening_seconds, podcast.pid),
    )
    ranking = tuple(
        PodcastRanking(
            rank=index,
            pid=podcast.pid,
            title=podcast.title,
            listening_seconds=podcast.total_listening_seconds,
        )
        for index, podcast in enumerate(ordered_podcasts, start=1)
    )
    day_seconds = {
        date.fromisoformat(key): value.listening_seconds for key, value in day_stats.items()
    }
    levels = _heat_levels(day_seconds)
    daily = tuple(
        DailyListening(
            day=day,
            listening_seconds=day_seconds[day],
            episode_count=day_stats[day.isoformat()].episode_count,
            podcast_count=day_stats[day.isoformat()].podcast_count,
            level=levels[day],
        )
        for day in sorted(day_seconds)
    )
    return StatisticsSnapshot(
        total=total,
        years=tuple(sorted(year_stats.values(), key=lambda value: value.key)),
        months=tuple(sorted(month_stats.values(), key=lambda value: value.key)),
        weeks=tuple(sorted(week_stats.values(), key=lambda value: value.key)),
        days=tuple(sorted(day_stats.values(), key=lambda value: value.key)),
        ranking=ranking,
        daily=daily,
    )
