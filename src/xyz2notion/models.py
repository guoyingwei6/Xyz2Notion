"""Stable domain contracts shared by providers and Notion rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


class ContractModel(BaseModel):
    """Strict immutable base for persisted cross-provider contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ListeningStatus(StrEnum):
    """Normalized listening state independent of Xiaoyuzhou response labels."""

    UNPLAYED = "unplayed"
    LISTENING = "listening"
    PLAYED = "played"


class PeriodKind(StrEnum):
    """Supported Notion statistics periods."""

    ALL = "all"
    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"


class ProviderErrorCategory(StrEnum):
    """Portable provider failure categories."""

    AUTHENTICATION = "authentication"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    NETWORK = "network"
    TIMEOUT = "timeout"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported"
    RISK_CONTROL = "risk_control"
    SCHEMA_CHANGED = "schema_changed"
    UNKNOWN = "unknown"


class TranscriptTimingQuality(StrEnum):
    """Precision of transcript timestamps supplied by a provider."""

    UNKNOWN = "unknown"
    COARSE = "coarse_timestamps"
    EXACT = "exact_timestamps"


_RETRYABLE_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.RATE_LIMITED,
        ProviderErrorCategory.UNAVAILABLE,
        ProviderErrorCategory.NETWORK,
        ProviderErrorCategory.TIMEOUT,
    }
)


class ProviderFailure(ContractModel):
    """Serializable provider error without raw credential-bearing responses."""

    provider: NonEmptyStr
    category: ProviderErrorCategory
    message: NonEmptyStr
    code: str | None = None

    @property
    def retryable(self) -> bool:
        """Whether this category is safe to retry automatically."""
        return self.category in _RETRYABLE_CATEGORIES


class ProviderError(RuntimeError):
    """Runtime exception carrying a safe structured provider failure."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class Author(ContractModel):
    """Podcast author or host."""

    author_id: NonEmptyStr
    name: NonEmptyStr
    avatar_url: str | None = None
    bio: str | None = None

    @property
    def idempotency_key(self) -> str:
        return f"author:{self.author_id}"


class Podcast(ContractModel):
    """Normalized podcast metadata."""

    pid: NonEmptyStr
    title: NonEmptyStr
    description: str = ""
    image_url: str | None = None
    author_ids: tuple[str, ...] = ()
    total_listening_seconds: NonNegativeInt = 0
    updated_at: AwareDatetime

    @property
    def idempotency_key(self) -> str:
        return podcast_key(self.pid)


class Episode(ContractModel):
    """Normalized episode metadata and listening progress."""

    eid: NonEmptyStr
    pid: NonEmptyStr
    title: NonEmptyStr
    description: str = ""
    image_url: str | None = None
    audio_url: str | None = None
    published_at: AwareDatetime
    duration_seconds: NonNegativeInt = 0
    played_seconds: NonNegativeInt = 0
    listening_status: ListeningStatus = ListeningStatus.UNPLAYED
    liked: bool = False
    favorited: bool = False
    in_playlist: bool = False
    last_played_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        if self.duration_seconds and self.played_seconds > self.duration_seconds:
            raise ValueError("played_seconds cannot exceed duration_seconds")
        return self

    @property
    def idempotency_key(self) -> str:
        return episode_key(self.eid)


class ListeningPeriod(ContractModel):
    """Aggregated listening statistics for one calendar period."""

    kind: PeriodKind
    key: NonEmptyStr
    start_date: date
    end_date: date
    listening_seconds: NonNegativeInt = 0
    episode_count: NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot be earlier than start_date")
        return self


class TranscriptSegment(ContractModel):
    """Timestamped transcript fragment."""

    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text: NonEmptyStr
    speaker: str | None = None

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("segment end_ms cannot be earlier than start_ms")
        return self


class TranscriptResult(ContractModel):
    """Provider-independent transcript result."""

    provider: NonEmptyStr
    provider_task_id: NonEmptyStr
    model: NonEmptyStr
    language: NonEmptyStr = "zh"
    duration_ms: NonNegativeInt
    text: NonEmptyStr
    segments: tuple[TranscriptSegment, ...] = ()
    timing_quality: TranscriptTimingQuality = TranscriptTimingQuality.UNKNOWN
    accuracy_hint: float | None = Field(default=None, ge=0, le=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_segment_order(self) -> Self:
        previous_end = 0
        for segment in self.segments:
            if segment.start_ms < previous_end:
                raise ValueError("transcript segments must be ordered and non-overlapping")
            previous_end = segment.end_ms
        return self

    @property
    def idempotency_key(self) -> str:
        return asr_task_key(self.provider, self.provider_task_id)


class Chapter(ContractModel):
    """AI-generated episode chapter."""

    start_ms: NonNegativeInt
    title: NonEmptyStr
    summary: str = ""


class MindmapNode(ContractModel):
    """Recursive mind-map node."""

    node_id: NonEmptyStr
    title: NonEmptyStr
    children: tuple[MindmapNode, ...] = ()


class SummaryResult(ContractModel):
    """Structured AI enrichment result."""

    summary: NonEmptyStr
    chapters: tuple[Chapter, ...] = ()
    highlights: tuple[str, ...] = ()
    quotes: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    mindmap: MindmapNode
    prompt_version: NonEmptyStr
    model: NonEmptyStr
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    estimated_cost_cny: float = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)


def _key(prefix: str, *parts: str) -> str:
    normalized = [part.strip() for part in parts]
    if any(not part for part in normalized):
        raise ValueError(f"{prefix} idempotency key contains an empty component")
    return ":".join((prefix, *normalized))


def podcast_key(pid: str) -> str:
    """Return the stable Notion upsert key for a podcast."""
    return _key("podcast", pid)


def episode_key(eid: str) -> str:
    """Return the stable Notion upsert key for an episode."""
    return _key("episode", eid)


def asr_task_key(provider: str, task_id: str) -> str:
    """Return the stable persisted key for a provider ASR task."""
    return _key("asr", provider, task_id)
