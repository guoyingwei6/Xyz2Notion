import json
from pathlib import Path

from xyz2notion.enrichment.text import chunk_transcript
from xyz2notion.models import (
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)


def test_six_synthetic_transcript_scenarios_survive_downstream_chunking() -> None:
    raw = json.loads(Path("tests/fixtures/transcript_cases.json").read_text(encoding="utf-8"))
    assert [case["case"] for case in raw] == [
        "mandarin_interview",
        "chinese_english_mixed",
        "dialect_visible",
        "multiple_speakers",
        "background_music_remote",
        "over_one_hour",
    ]
    for index, case in enumerate(raw):
        segments = tuple(TranscriptSegment.model_validate(segment) for segment in case["segments"])
        transcript = TranscriptResult(
            provider="synthetic",
            provider_task_id=f"case-{index}",
            model="fixture",
            duration_ms=case["duration_ms"],
            text="\n".join(segment.text for segment in segments),
            segments=segments,
            timing_quality=TranscriptTimingQuality.EXACT,
        )
        chunks = chunk_transcript(
            transcript,
            max_tokens=1_000,
            max_duration_ms=30 * 60 * 1_000,
        )
        assert chunks
        assert "".join(chunk.text for chunk in chunks).replace("\n", "")
        assert chunks[-1].end_ms == transcript.duration_ms

    assert raw[-1]["duration_ms"] > 60 * 60 * 1_000
