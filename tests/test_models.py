from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from xyz2notion.models import (
    Author,
    Chapter,
    Episode,
    ListeningPeriod,
    ListeningStatus,
    MindmapNode,
    PeriodKind,
    Podcast,
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    SummaryResult,
    TranscriptResult,
    TranscriptSegment,
    asr_task_key,
    episode_key,
    podcast_key,
)

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


def domain_examples() -> tuple[object, ...]:
    mindmap = MindmapNode(
        node_id="root",
        title="主题",
        children=(MindmapNode(node_id="child", title="观点"),),
    )
    return (
        Author(author_id="author-1", name="主播"),
        Podcast(pid="podcast-1", title="播客", updated_at=NOW),
        Episode(
            eid="episode-1",
            pid="podcast-1",
            title="单集",
            published_at=NOW,
            duration_seconds=600,
            played_seconds=120,
            listening_status=ListeningStatus.LISTENING,
        ),
        ListeningPeriod(
            kind=PeriodKind.DAY,
            key="2026-07-29",
            start_date=date(2026, 7, 29),
            end_date=date(2026, 7, 29),
            listening_seconds=120,
            episode_count=1,
        ),
        TranscriptResult(
            provider="siliconflow",
            provider_task_id="task-1",
            model="SenseVoiceSmall",
            duration_ms=2000,
            text="第一句。第二句。",
            segments=(
                TranscriptSegment(start_ms=0, end_ms=1000, text="第一句。"),
                TranscriptSegment(start_ms=1000, end_ms=2000, text="第二句。"),
            ),
            accuracy_hint=0.9,
            created_at=NOW,
        ),
        SummaryResult(
            summary="节目摘要",
            chapters=(Chapter(start_ms=0, title="开场"),),
            highlights=("重点",),
            questions=("问题?",),
            mindmap=mindmap,
            prompt_version="v1",
            model="qwen",
            created_at=NOW,
        ),
        ProviderFailure(
            provider="siliconflow",
            category=ProviderErrorCategory.TIMEOUT,
            message="provider timed out",
            code="timeout",
        ),
    )


@pytest.mark.parametrize("instance", domain_examples())
def test_all_domain_models_round_trip_json(instance: object) -> None:
    model_type = type(instance)
    payload = instance.model_dump_json()  # type: ignore[attr-defined]
    assert model_type.model_validate_json(payload) == instance  # type: ignore[attr-defined]


def test_idempotency_keys_are_stable() -> None:
    assert podcast_key(" p1 ") == "podcast:p1"
    assert episode_key("e1") == "episode:e1"
    assert asr_task_key("siliconflow", "task-1") == "asr:siliconflow:task-1"
    podcast = Podcast(pid="p1", title="播客", updated_at=NOW)
    episode = Episode(eid="e1", pid="p1", title="单集", published_at=NOW)
    transcript = TranscriptResult(
        provider="siliconflow",
        provider_task_id="task-1",
        model="model",
        duration_ms=1,
        text="文字",
    )
    author = Author(author_id="a1", name="主播")
    assert author.idempotency_key == "author:a1"
    assert podcast.idempotency_key == "podcast:p1"
    assert episode.idempotency_key == "episode:e1"
    assert transcript.idempotency_key == "asr:siliconflow:task-1"
    with pytest.raises(ValueError, match="empty component"):
        episode_key(" ")


def test_episode_progress_cannot_exceed_duration() -> None:
    with pytest.raises(ValidationError, match="played_seconds"):
        Episode(
            eid="e1",
            pid="p1",
            title="单集",
            published_at=NOW,
            duration_seconds=10,
            played_seconds=11,
        )


def test_period_dates_are_ordered() -> None:
    with pytest.raises(ValidationError, match="end_date"):
        ListeningPeriod(
            kind=PeriodKind.DAY,
            key="bad",
            start_date=date(2026, 7, 30),
            end_date=date(2026, 7, 29),
        )


def test_transcript_segments_validate_time_and_order() -> None:
    with pytest.raises(ValidationError, match="end_ms"):
        TranscriptSegment(start_ms=2, end_ms=1, text="bad")
    with pytest.raises(ValidationError, match="non-overlapping"):
        TranscriptResult(
            provider="provider",
            provider_task_id="task",
            model="model",
            duration_ms=10,
            text="text",
            segments=(
                TranscriptSegment(start_ms=0, end_ms=7, text="one"),
                TranscriptSegment(start_ms=6, end_ms=10, text="two"),
            ),
        )


def test_provider_failure_category_controls_retry() -> None:
    retryable = ProviderFailure(
        provider="provider",
        category=ProviderErrorCategory.RATE_LIMITED,
        message="retry later",
    )
    final = ProviderFailure(
        provider="provider",
        category=ProviderErrorCategory.AUTHENTICATION,
        message="refresh credentials",
    )
    assert retryable.retryable is True
    assert final.retryable is False
    error = ProviderError(final)
    assert error.failure is final
    assert str(error) == "refresh credentials"
