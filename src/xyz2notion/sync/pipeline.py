"""Read-only Xiaoyuzhou discovery for one metadata synchronization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from xyz2notion.sync.normalizer import MetadataSnapshot, build_metadata_snapshot

JsonObject = dict[str, Any]


class XiaoyuzhouMetadataAPI(Protocol):
    """Private API methods required for metadata discovery."""

    def subscriptions(self, *, limit: int = 25) -> list[JsonObject]: ...

    def mileage(self, *, rank: str = "TOTAL") -> list[JsonObject]: ...

    def podcast(self, pid: str) -> JsonObject: ...

    def episodes(self, pid: str, *, limit: int = 25) -> list[JsonObject]: ...

    def play_history(self, *, limit: int = 25) -> list[JsonObject]: ...

    def playback_progress(
        self,
        eids: Sequence[str],
        *,
        batch_size: int = 100,
    ) -> list[JsonObject]: ...


def _nested_id(item: Mapping[str, Any], entity: str, key: str) -> str:
    nested = item.get(entity)
    source = nested if isinstance(nested, Mapping) else item
    value = source.get(key)
    return str(value).strip() if value is not None else ""


def collect_metadata(api: XiaoyuzhouMetadataAPI) -> MetadataSnapshot:
    """Collect subscriptions, listened podcasts, all episodes, history, and progress."""
    subscriptions = api.subscriptions()
    mileage = api.mileage()
    history = api.play_history()
    known_pids = {
        pid for item in (*subscriptions, *mileage) if (pid := _nested_id(item, "podcast", "pid"))
    }
    history_pids = {pid for item in history if (pid := _nested_id(item, "episode", "pid"))}
    recovered_podcasts = [api.podcast(pid) for pid in sorted(history_pids - known_pids)]
    subscriptions = [*subscriptions, *recovered_podcasts]
    pids = tuple(
        dict.fromkeys(
            pid
            for item in (*subscriptions, *mileage, *history)
            if (pid := _nested_id(item, "podcast", "pid"))
            or (pid := _nested_id(item, "episode", "pid"))
        )
    )
    episodes = [episode for pid in pids for episode in api.episodes(pid)]
    combined_episodes = [*episodes, *history]
    eids = tuple(
        dict.fromkeys(
            eid for item in combined_episodes if (eid := _nested_id(item, "episode", "eid"))
        )
    )
    progress = api.playback_progress(eids) if eids else []
    return build_metadata_snapshot(
        subscriptions,
        mileage,
        combined_episodes,
        progress,
    )
