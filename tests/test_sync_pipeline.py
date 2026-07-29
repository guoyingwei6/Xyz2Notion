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


def test_collect_monthly_wrapped_fetches_history_but_not_current_month() -> None:
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
    assert [(value.year, value.month) for value in values] == [
        (2025, 12),
        (2026, 1),
    ]
    assert values[0].listening_seconds == 2037
