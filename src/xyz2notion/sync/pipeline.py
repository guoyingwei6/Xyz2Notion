"""Read-only Xiaoyuzhou discovery for one metadata synchronization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Protocol

from xyz2notion.statistics.calculator import MonthlyWrappedValue
from xyz2notion.sync.normalizer import MetadataSnapshot, build_metadata_snapshot

JsonObject = dict[str, Any]
MAX_PLAYLIST_HYDRATIONS = 3
MAX_PODCAST_RECOVERIES = 2
MAX_PROGRESS_EPISODES = 25


class XiaoyuzhouMetadataAPI(Protocol):
    """Private API methods required for metadata discovery."""

    def subscriptions(self, *, limit: int = 25) -> list[JsonObject]: ...

    def mileage(self, *, rank: str = "TOTAL") -> list[JsonObject]: ...

    def podcast(self, pid: str) -> JsonObject: ...

    def play_history(self, *, limit: int = 25) -> list[JsonObject]: ...

    def playlist_eids(self) -> list[str]: ...

    def favorites(self) -> list[JsonObject]: ...

    def episode(self, eid: str) -> JsonObject: ...

    def playback_progress(
        self,
        eids: Sequence[str],
        *,
        batch_size: int = 25,
    ) -> list[JsonObject]: ...

    def profile(self) -> JsonObject: ...

    def monthly_wrapped(
        self,
        year: int,
        month: int,
        *,
        uid: str | None = None,
    ) -> JsonObject: ...


def _nested_id(item: Mapping[str, Any], entity: str, key: str) -> str:
    nested = item.get(entity)
    source = nested if isinstance(nested, Mapping) else item
    value = source.get(key)
    return str(value).strip() if value is not None else ""


def collect_metadata(
    api: XiaoyuzhouMetadataAPI,
) -> MetadataSnapshot:
    """Collect only playback-history episodes with positive listening progress."""
    subscriptions = api.subscriptions()
    mileage = api.mileage()
    history = api.play_history()
    playlist_eids = api.playlist_eids()
    favorites = api.favorites()
    history_eids = {eid for item in history if (eid := _nested_id(item, "episode", "eid"))}
    favorite_eids = {
        eid
        for item in favorites
        if (eid := _nested_id(item, "episode", "eid"))
        or (eid := str(item.get("eid") or "").strip())
    }
    playlist_only_eids = [
        eid for eid in playlist_eids if eid not in history_eids and eid not in favorite_eids
    ][:MAX_PLAYLIST_HYDRATIONS]
    playlist_only = [api.episode(eid) for eid in playlist_only_eids]
    playlist_set = set(playlist_eids)
    playlist_positions = {eid: position for position, eid in enumerate(playlist_eids, start=1)}
    favorite_set = set(favorite_eids)

    combined_episodes: list[JsonObject] = []
    for item in [*history, *playlist_only, *favorites]:
        wrapper = dict(item)
        raw = wrapper.get("episode")
        episode = dict(raw) if isinstance(raw, Mapping) else dict(wrapper)
        eid = str(episode.get("eid") or "").strip()
        if not eid:
            continue
        episode["_xyz_in_playlist"] = eid in playlist_set
        episode["_xyz_playlist_position"] = playlist_positions.get(eid)
        episode["_xyz_favorited"] = eid in favorite_set or bool(episode.get("isFavorited"))
        combined_episodes.append({"episode": episode})
    known_pids = {
        pid for item in (*subscriptions, *mileage) if (pid := _nested_id(item, "podcast", "pid"))
    }
    history_pids = {
        pid for item in combined_episodes if (pid := _nested_id(item, "episode", "pid"))
    }
    recovered_podcasts = [
        api.podcast(pid) for pid in sorted(history_pids - known_pids)[:MAX_PODCAST_RECOVERIES]
    ]
    subscriptions = [*subscriptions, *recovered_podcasts]
    eids = tuple(
        dict.fromkeys(
            eid for item in combined_episodes if (eid := _nested_id(item, "episode", "eid"))
        )
    )
    progress = api.playback_progress(eids[:MAX_PROGRESS_EPISODES], batch_size=25) if eids else []
    return build_metadata_snapshot(
        subscriptions,
        mileage,
        combined_episodes,
        progress,
    )


def collect_monthly_wrapped(
    api: XiaoyuzhouMetadataAPI,
    snapshot: MetadataSnapshot,
    *,
    today: date | None = None,
) -> tuple[MonthlyWrappedValue, ...]:
    """Fetch only the previous complete month's official total."""
    current_day = today or date.today()
    if not any(episode.played_seconds > 0 for episode in snapshot.episodes):
        return ()
    profile = api.profile()
    uid = str(profile.get("uid") or "")
    if not uid:
        return ()
    if current_day.month == 1:
        year, month = current_day.year - 1, 12
    else:
        year, month = current_day.year, current_day.month - 1
    payload = api.monthly_wrapped(year, month, uid=uid)
    return (
        MonthlyWrappedValue(
            year=year,
            month=month,
            listening_seconds=max(0, int(payload.get("playedSeconds") or 0)),
            played_days=max(0, int(payload.get("playedDays") or 0)),
        ),
    )
