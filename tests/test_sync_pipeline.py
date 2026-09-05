from collections.abc import Sequence
from datetime import UTC, date, datetime

from xyz2notion.models import Episode, ListeningStatus
from xyz2notion.notion.client import JsonObject
from xyz2notion.sync.normalizer import MetadataSnapshot
from xyz2notion.sync.pipeline import collect_metadata, collect_monthly_wrapped


class FakeXiaoyuzhou:
    def __init__(self) -> None:
        self.requested_pids: list[str] = []
        self.progress_eids: tuple[str, ...] = ()

    def subscriptions(self, *, limit: int = 25) -> list[JsonObject]:
        assert limit == 25
        return [
            {
                "pid": "subscribed-podcast",
                "title": "Subscribed",
                "latestEpisodePubDate": "2026-01-01T00:00:00Z",
            }
        ]

    def mileage(self, *, rank: str = "TOTAL") -> list[JsonObject]:
        assert rank == "TOTAL"
        return [
            {
                "podcast": {
                    "pid": "listened-podcast",
                    "title": "Listened",
                },
                "playedSeconds": 99,
            }
        ]

    def podcast(self, pid: str) -> JsonObject:
        assert pid == "history-podcast"
        return {"pid": pid, "title": "Recovered History Podcast"}

    def episode(self, eid: str) -> JsonObject:
        raise AssertionError(eid)

    def episodes(self, pid: str, *, limit: int = 25) -> list[JsonObject]:
        assert limit == 25
        self.requested_pids.append(pid)
        return [
            {
                "eid": f"episode-{pid}",
                "pid": pid,
                "title": f"Episode {pid}",
                "pubDate": "2026-01-01T00:00:00Z",
            }
        ]

    def play_history(self, *, limit: int = 25) -> list[JsonObject]:
        assert limit == 25
        return [
            {
                "episode": {
                    "eid": "history-episode",
                    "pid": "history-podcast",
                    "title": "History",
                    "pubDate": "2026-01-01T00:00:00Z",
                }
            }
        ]

    def playlist_eids(self) -> list[str]:
        return []

    def favorites(self) -> list[JsonObject]:
        return []

    def playback_progress(
        self,
        eids: Sequence[str],
        *,
        batch_size: int = 25,
    ) -> list[JsonObject]:
        assert batch_size == 25
        self.progress_eids = tuple(eids)
        return [{"eid": eid, "progress": 1} for eid in eids]

    def profile(self) -> JsonObject:
        return {"uid": "user-fixture"}

    def monthly_wrapped(
        self,
        year: int,
        month: int,
        *,
        uid: str | None = None,
    ) -> JsonObject:
        assert uid == "user-fixture"
        return {"playedSeconds": year + month, "playedDays": month}


def test_collect_metadata_defaults_to_listened_history_only() -> None:
    fake = FakeXiaoyuzhou()
    snapshot = collect_metadata(fake)
    assert fake.requested_pids == []
    assert fake.progress_eids == ("history-episode",)
    assert {podcast.pid for podcast in snapshot.podcasts} == {"history-podcast"}
    assert {episode.eid for episode in snapshot.episodes} == {"history-episode"}


def test_history_wrapper_played_at_survives_collection() -> None:
    class DatedHistory(FakeXiaoyuzhou):
        def play_history(self, *, limit: int = 25) -> list[JsonObject]:
            rows = super().play_history(limit=limit)
            rows[0]["playedAt"] = "2026-09-04T08:00:00Z"
            return rows

    snapshot = collect_metadata(DatedHistory())
    assert snapshot.episodes[0].last_played_at == datetime(2026, 9, 4, 8, tzinfo=UTC)


def test_collect_metadata_drops_history_rows_without_playback_progress() -> None:
    fake = FakeXiaoyuzhou()
    fake.playback_progress = lambda _eids, batch_size=25: []  # type: ignore[method-assign]
    snapshot = collect_metadata(fake)
    assert snapshot.episodes == ()
    assert snapshot.podcasts == ()
    assert snapshot.authors == ()


def test_collect_metadata_keeps_playlist_and_favorites_as_non_statistical_rows() -> None:
    class WithLibrary(FakeXiaoyuzhou):
        def play_history(self, *, limit: int = 25) -> list[JsonObject]:
            history = super().play_history(limit=limit)
            history.append(
                {
                    "episode": {
                        "eid": "liked-episode",
                        "pid": "liked-podcast",
                        "title": "Liked",
                        "pubDate": "2026-01-04T00:00:00Z",
                        "isPicked": True,
                    }
                }
            )
            return history

        def playlist_eids(self) -> list[str]:
            return ["playlist-episode"]

        def favorites(self) -> list[JsonObject]:
            return [
                {
                    "eid": "favorite-episode",
                    "pid": "favorite-podcast",
                    "title": "Favorite",
                    "pubDate": "2026-01-02T00:00:00Z",
                    "isFavorited": True,
                }
            ]

        def episode(self, eid: str) -> JsonObject:
            assert eid == "playlist-episode"
            return {
                "eid": eid,
                "pid": "playlist-podcast",
                "title": "Listen later",
                "pubDate": "2026-01-03T00:00:00Z",
            }

        def podcast(self, pid: str) -> JsonObject:
            return {"pid": pid, "title": f"Podcast {pid}"}

        def playback_progress(
            self,
            eids: Sequence[str],
            *,
            batch_size: int = 25,
        ) -> list[JsonObject]:
            assert batch_size == 25
            return [
                {
                    "eid": eid,
                    "progress": 1 if eid == "history-episode" else 0,
                }
                for eid in eids
            ]

    snapshot = collect_metadata(WithLibrary())
    by_eid = {episode.eid: episode for episode in snapshot.episodes}
    assert set(by_eid) == {
        "history-episode",
        "playlist-episode",
        "favorite-episode",
        "liked-episode",
    }
    assert by_eid["playlist-episode"].in_playlist is True
    assert by_eid["playlist-episode"].played_seconds == 0
    assert by_eid["favorite-episode"].favorited is True
    assert by_eid["favorite-episode"].played_seconds == 0
    assert by_eid["liked-episode"].liked is True
    assert by_eid["liked-episode"].played_seconds == 0


def test_collect_metadata_preserves_playlist_order() -> None:
    class WithOrderedPlaylist(FakeXiaoyuzhou):
        def playlist_eids(self) -> list[str]:
            return ["playlist-second", "playlist-first"]

        def episode(self, eid: str) -> JsonObject:
            return {
                "eid": eid,
                "pid": "playlist-podcast",
                "title": eid,
                "pubDate": "2026-01-03T00:00:00Z",
            }

        def podcast(self, pid: str) -> JsonObject:
            return {"pid": pid, "title": f"Podcast {pid}"}

        def playback_progress(
            self,
            eids: Sequence[str],
            *,
            batch_size: int = 25,
        ) -> list[JsonObject]:
            return [{"eid": eid, "progress": 1 if eid == "history-episode" else 0} for eid in eids]

    by_eid = {episode.eid: episode for episode in collect_metadata(WithOrderedPlaylist()).episodes}
    assert by_eid["playlist-second"].playlist_position == 1
    assert by_eid["playlist-first"].playlist_position == 2


def test_collect_monthly_wrapped_fetches_only_previous_complete_month() -> None:
    fake = FakeXiaoyuzhou()
    snapshot = MetadataSnapshot(
        authors=(),
        podcasts=(),
        episodes=(
            Episode(
                eid="e1",
                pid="p1",
                title="Episode",
                published_at=datetime(2025, 11, 1, tzinfo=UTC),
                duration_seconds=100,
                played_seconds=50,
                listening_status=ListeningStatus.LISTENING,
                last_played_at=datetime(2025, 12, 2, tzinfo=UTC),
            ),
        ),
    )
    values = collect_monthly_wrapped(fake, snapshot, today=date(2026, 2, 15))
    assert [(value.year, value.month) for value in values] == [(2026, 1)]
    assert values[0].listening_seconds == 2027


def test_collect_metadata_caps_per_item_recovery_work() -> None:
    class LargeLibrary(FakeXiaoyuzhou):
        def __init__(self) -> None:
            super().__init__()
            self.requested_eids: list[str] = []
            self.recovered_pids: list[str] = []

        def playlist_eids(self) -> list[str]:
            return [f"playlist-{index}" for index in range(10)]

        def episode(self, eid: str) -> JsonObject:
            self.requested_eids.append(eid)
            return {
                "eid": eid,
                "pid": f"podcast-{eid}",
                "title": eid,
                "pubDate": "2026-01-03T00:00:00Z",
            }

        def podcast(self, pid: str) -> JsonObject:
            self.recovered_pids.append(pid)
            return {"pid": pid, "title": pid}

    fake = LargeLibrary()
    collect_metadata(fake)
    assert fake.requested_eids == ["playlist-0", "playlist-1", "playlist-2"]
    assert len(fake.recovered_pids) == 2
