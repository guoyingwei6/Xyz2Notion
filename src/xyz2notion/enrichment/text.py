"""Transcript cleanup, timestamp preservation, and bounded chunk planning."""

from __future__ import annotations

import re
from dataclasses import dataclass

from xyz2notion.models import TranscriptResult, TranscriptSegment

_NOISE_ONLY = re.compile(
    r"^\s*[\[【（(]\s*(?:音乐|掌声|笑声|广告|片头|片尾|music|applause|laughter)\s*[\]】）)]\s*$",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"[ \t\u3000]+")


@dataclass(frozen=True)
class TranscriptChunk:
    """A prompt-safe transcript range mapped back to the original timeline."""

    index: int
    start_ms: int
    end_ms: int
    text: str
    estimated_tokens: int


def clean_segment_text(text: str) -> str:
    """Remove standalone event labels and normalize horizontal whitespace."""
    stripped = text.strip()
    if not stripped or _NOISE_ONLY.fullmatch(stripped):
        return ""
    return _WHITESPACE.sub(" ", stripped)


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate suitable for Chinese/English mixes."""
    if not text:
        return 0
    ascii_count = sum(character.isascii() for character in text)
    non_ascii_count = len(text) - ascii_count
    return max(1, non_ascii_count + (ascii_count + 3) // 4)


def _timestamp(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _render(segment: TranscriptSegment, text: str) -> str:
    speaker = f" {segment.speaker}" if segment.speaker else ""
    return f"[{_timestamp(segment.start_ms)}{speaker}] {text}"


def _split_segment(
    segment: TranscriptSegment,
    text: str,
    max_tokens: int,
) -> tuple[str, ...]:
    """Find the largest text slices whose rendered rows stay inside the budget."""
    parts: list[str] = []
    offset = 0
    while offset < len(text):
        low = offset + 1
        high = len(text)
        best = low
        while low <= high:
            middle = (low + high) // 2
            candidate = _render(segment, text[offset:middle])
            if estimate_tokens(candidate) <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        parts.append(_render(segment, text[offset:best]))
        offset = best
    return tuple(parts)


def chunk_transcript(
    transcript: TranscriptResult,
    *,
    max_tokens: int = 24_000,
    max_duration_ms: int = 30 * 60 * 1000,
) -> tuple[TranscriptChunk, ...]:
    """Split on segment/time/token boundaries while retaining source timestamps."""
    if max_tokens < 1 or max_duration_ms < 1:
        raise ValueError("chunk limits must be positive")
    source_segments = transcript.segments or (
        TranscriptSegment(
            start_ms=0,
            end_ms=transcript.duration_ms,
            text=transcript.text,
        ),
    )
    rows: list[tuple[int, int, str]] = []
    for segment in source_segments:
        cleaned = clean_segment_text(segment.text)
        if not cleaned:
            continue
        rendered = _render(segment, cleaned)
        rendered_tokens = estimate_tokens(rendered)
        if rendered_tokens <= max_tokens:
            rows.append((segment.start_ms, segment.end_ms, rendered))
            continue
        for part in _split_segment(segment, cleaned, max_tokens):
            rows.append((segment.start_ms, segment.end_ms, part))

    chunks: list[TranscriptChunk] = []
    buffer: list[str] = []
    token_count = 0
    start_ms = 0
    end_ms = 0
    for row_start, row_end, row_text in rows:
        row_tokens = estimate_tokens(row_text)
        duration_exceeded = bool(buffer) and row_end - start_ms > max_duration_ms
        tokens_exceeded = bool(buffer) and token_count + row_tokens > max_tokens
        if duration_exceeded or tokens_exceeded:
            chunks.append(
                TranscriptChunk(
                    index=len(chunks) + 1,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text="\n".join(buffer),
                    estimated_tokens=token_count,
                )
            )
            buffer = []
            token_count = 0
        if not buffer:
            start_ms = row_start
        buffer.append(row_text)
        token_count += row_tokens
        end_ms = row_end
    if buffer:
        chunks.append(
            TranscriptChunk(
                index=len(chunks) + 1,
                start_ms=start_ms,
                end_ms=end_ms,
                text="\n".join(buffer),
                estimated_tokens=token_count,
            )
        )
    return tuple(chunks)
