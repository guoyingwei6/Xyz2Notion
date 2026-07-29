"""Ephemeral SiliconFlow episode transcription orchestration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from xyz2notion.asr.audio import (
    AudioPreprocessor,
    PreparedAudio,
    download_audio,
)
from xyz2notion.models import TranscriptResult


class AudioPreparationAPI(Protocol):
    def prepare(self, source: Path, workdir: Path) -> PreparedAudio: ...


class TranscriptProviderAPI(Protocol):
    def transcribe(self, prepared: PreparedAudio) -> TranscriptResult: ...


def transcribe_siliconflow_episode(
    audio_url: str,
    provider: TranscriptProviderAPI,
    *,
    preprocessor: AudioPreparationAPI | None = None,
    http_client: httpx.Client | None = None,
) -> TranscriptResult:
    """Download, normalize, split, transcribe, and remove all audio on exit."""
    processor = preprocessor or AudioPreprocessor()
    with tempfile.TemporaryDirectory(prefix="xyz2notion-audio-") as temporary:
        workdir = Path(temporary)
        source = download_audio(
            audio_url,
            workdir / "source.audio",
            client=http_client,
        )
        prepared = processor.prepare(source, workdir / "prepared")
        return provider.transcribe(prepared)
