from collections.abc import Sequence

from xyz2notion.notion.client import JsonObject
from xyz2notion.sync.pipeline import collect_metadata


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

    def playback_progress(
        self,
        eids: Sequence[str],
        *,
        batch_size: int = 100,
    ) -> list[JsonObject]:
        assert batch_size == 100
        self.progress_eids = tuple(eids)
        return [{"eid": eid, "progress": 1} for eid in eids]


def test_collect_metadata_covers_subscriptions_mileage_history_and_all_episodes() -> None:
    fake = FakeXiaoyuzhou()
    snapshot = collect_metadata(fake)
    assert set(fake.requested_pids) == {
        "subscribed-podcast",
        "listened-podcast",
        "history-podcast",
    }
    assert set(fake.progress_eids) == {
        "episode-subscribed-podcast",
        "episode-listened-podcast",
        "episode-history-podcast",
        "history-episode",
    }
    assert {podcast.pid for podcast in snapshot.podcasts} == {
        "subscribed-podcast",
        "listened-podcast",
        "history-podcast",
    }
    assert {episode.eid for episode in snapshot.episodes} == set(fake.progress_eids)
