from xyz2notion.enrichment.text import (
    chunk_transcript,
    clean_segment_text,
    estimate_tokens,
)
from xyz2notion.models import TranscriptResult, TranscriptSegment


def transcript_with_segments() -> TranscriptResult:
    return TranscriptResult(
        provider="fixture",
        provider_task_id="task",
        model="model",
        duration_ms=3_000,
        text="fallback",
        segments=(
            TranscriptSegment(start_ms=0, end_ms=1_000, text=" [音乐] "),
            TranscriptSegment(
                start_ms=1_000,
                end_ms=2_000,
                text="第一句   with spaces",
                speaker="主播",
            ),
            TranscriptSegment(start_ms=2_000, end_ms=3_000, text="第二句"),
        ),
    )


def test_cleanup_and_token_estimation() -> None:
    assert clean_segment_text("[音乐]") == ""
    assert clean_segment_text("(applause)") == ""
    assert clean_segment_text("  中文   English  ") == "中文 English"
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcdefgh") == 2


def test_chunks_preserve_timestamp_speaker_and_remove_noise() -> None:
    chunks = chunk_transcript(transcript_with_segments(), max_tokens=20)
    assert len(chunks) == 1
    assert chunks[0].start_ms == 1_000
    assert chunks[0].text.startswith("[00:00:01 主播]")
    assert "音乐" not in "\n".join(chunk.text for chunk in chunks)
    assert [chunk.index for chunk in chunks] == [1]


def test_time_boundary_and_oversized_segment_are_split() -> None:
    transcript = TranscriptResult(
        provider="fixture",
        provider_task_id="task",
        model="model",
        duration_ms=20_000,
        text="unused",
        segments=(
            TranscriptSegment(start_ms=0, end_ms=1_000, text="甲"),
            TranscriptSegment(start_ms=10_000, end_ms=11_000, text="乙"),
            TranscriptSegment(start_ms=11_000, end_ms=12_000, text="很长" * 50),
        ),
    )
    chunks = chunk_transcript(transcript, max_tokens=20, max_duration_ms=5_000)
    assert len(chunks) > 3
    assert chunks[0].end_ms == 1_000
    assert all(chunk.estimated_tokens <= 20 for chunk in chunks)


def test_transcript_without_segments_uses_full_text_and_empty_noise_stays_empty() -> None:
    transcript = TranscriptResult(
        provider="fixture",
        provider_task_id="task",
        model="model",
        duration_ms=5_000,
        text="完整文字稿",
    )
    chunks = chunk_transcript(transcript)
    assert len(chunks) == 1
    assert "完整文字稿" in chunks[0].text

    noise = transcript.model_copy(update={"text": "[片头]"})
    assert chunk_transcript(noise) == ()


def test_chunk_limits_must_be_positive() -> None:
    transcript = transcript_with_segments()
    for tokens, duration in ((0, 1), (1, 0)):
        try:
            chunk_transcript(transcript, max_tokens=tokens, max_duration_ms=duration)
        except ValueError as error:
            assert "positive" in str(error)
        else:
            raise AssertionError("invalid limits were accepted")
