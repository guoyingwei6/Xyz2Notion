"""Normalize private Xiaoyuzhou payloads into stable domain contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from xyz2notion.models import Author, Episode, ListeningStatus, Podcast

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class MetadataSnapshot:
    """One normalized point-in-time snapshot ready for Notion."""

    authors: tuple[Author, ...]
    podcasts: tuple[Podcast, ...]
    episodes: tuple[Episode, ...]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _integer(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return 0


def _datetime(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, int | float):
        result = datetime.fromtimestamp(value, tz=UTC)
    else:
        rendered = _text(value)
        if not rendered:
            return fallback
        if rendered.endswith("Z"):
            rendered = f"{rendered[:-1]}+00:00"
        try:
            result = datetime.fromisoformat(rendered)
        except ValueError:
            return fallback
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _picture_url(value: object) -> str | None:
    image = _mapping(value)
    picture = _mapping(image.get("picture"))
    result = (
        picture.get("picUrl")
        or picture.get("largePicUrl")
        or picture.get("middlePicUrl")
        or picture.get("smallPicUrl")
        or image.get("picUrl")
    )
    rendered = _text(result)
    return rendered or None


def _stable_author_id(raw: Mapping[str, Any], name: str) -> str:
    direct = raw.get("uid") or raw.get("authorId") or raw.get("id")
    if direct:
        return _text(direct)
    digest = hashlib.sha256(f"xiaoyuzhou-author:{name}".encode()).hexdigest()
    return f"name-sha256:{digest}"


def _podcast_payload(item: Mapping[str, Any]) -> JsonObject:
    nested = item.get("podcast")
    podcast = dict(nested) if isinstance(nested, Mapping) else dict(item)
    if "playedSeconds" in item:
        podcast["playedSeconds"] = item["playedSeconds"]
    return podcast


def _episode_payload(item: Mapping[str, Any]) -> JsonObject:
    nested = item.get("episode")
    episode = dict(nested) if isinstance(nested, Mapping) else dict(item)
    if item.get("playedAt") and not episode.get("playedAt"):
        episode["playedAt"] = item["playedAt"]
    return episode


def _normalize_status(played_seconds: int, duration_seconds: int) -> ListeningStatus:
    if played_seconds <= 0:
        return ListeningStatus.UNPLAYED
    if duration_seconds and played_seconds >= max(1, duration_seconds - 15):
        return ListeningStatus.PLAYED
    return ListeningStatus.LISTENING


def build_metadata_snapshot(
    subscriptions: Sequence[Mapping[str, Any]],
    mileage: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    progress: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> MetadataSnapshot:
    """Merge API lists without mutating them and normalize stable entities."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    podcasts_by_pid: dict[str, JsonObject] = {}
    for source in (*subscriptions, *mileage):
        podcast = _podcast_payload(source)
        pid = _text(podcast.get("pid"))
        if not pid:
            continue
        merged = {**podcasts_by_pid.get(pid, {}), **podcast}
        podcasts_by_pid[pid] = merged

    authors_by_id: dict[str, Author] = {}
    podcasts: list[Podcast] = []
    for pid, raw in podcasts_by_pid.items():
        author_ids: list[str] = []
        podcasters = raw.get("podcasters")
        if isinstance(podcasters, Sequence) and not isinstance(podcasters, str | bytes):
            for value in podcasters:
                author_raw = _mapping(value)
                name = _text(author_raw.get("nickname") or author_raw.get("name"))
                if not name:
                    continue
                author_id = _stable_author_id(author_raw, name)
                author_ids.append(author_id)
                authors_by_id[author_id] = Author(
                    author_id=author_id,
                    name=name,
                    avatar_url=_picture_url(author_raw.get("avatar")),
                    bio=_text(author_raw.get("bio")) or None,
                )
        title = _text(raw.get("title")) or f"Podcast {pid}"
        podcasts.append(
            Podcast(
                pid=pid,
                title=title,
                description=_text(raw.get("description") or raw.get("brief")),
                image_url=_picture_url(raw.get("image")),
                author_ids=tuple(dict.fromkeys(author_ids)),
                total_listening_seconds=_integer(raw.get("playedSeconds")),
                updated_at=_datetime(
                    raw.get("latestEpisodePubDate") or raw.get("updatedAt"),
                    fallback=current,
                ),
            )
        )

    progress_by_eid = {_text(item.get("eid")): item for item in progress if _text(item.get("eid"))}
    podcast_images = {podcast.pid: podcast.image_url for podcast in podcasts}
    episodes_by_eid: dict[str, Episode] = {}
    for wrapper in history:
        raw = _episode_payload(wrapper)
        eid = _text(raw.get("eid"))
        pid = _text(raw.get("pid"))
        if not eid or not pid:
            continue
        progress_raw = progress_by_eid.get(eid, {})
        duration = _integer(raw.get("duration"))
        played = _integer(progress_raw.get("progress") or raw.get("progress"))
        played = min(played, duration) if duration else played
        published_at = _datetime(raw.get("pubDate"), fallback=current)
        played_at_value = progress_raw.get("playedAt") or raw.get("playedAt")
        last_played_at = (
            _datetime(played_at_value, fallback=published_at) if played_at_value else None
        )
        media = _mapping(raw.get("media"))
        source = _mapping(media.get("source"))
        episodes_by_eid[eid] = Episode(
            eid=eid,
            pid=pid,
            title=_text(raw.get("title")) or f"Episode {eid}",
            description=_text(raw.get("description")),
            image_url=_picture_url(raw.get("image")) or podcast_images.get(pid),
            audio_url=_text(source.get("url")) or None,
            published_at=published_at,
            duration_seconds=duration,
            played_seconds=played,
            listening_status=_normalize_status(played, duration),
            liked=bool(raw.get("isPicked")),
            last_played_at=last_played_at,
        )

    return MetadataSnapshot(
        authors=tuple(sorted(authors_by_id.values(), key=lambda value: value.author_id)),
        podcasts=tuple(sorted(podcasts, key=lambda value: value.pid)),
        episodes=tuple(sorted(episodes_by_eid.values(), key=lambda value: value.eid)),
    )
