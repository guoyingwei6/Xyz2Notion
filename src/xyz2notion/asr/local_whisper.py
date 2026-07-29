"""Last-resort local Whisper transcription on the GitHub Actions CPU runner."""

from __future__ import annotations

import hashlib
import os
from importlib import import_module
from pathlib import Path
from typing import Any

from xyz2notion.asr.audio import PreparedAudio
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)

SUPPORTED_MODELS = frozenset({"tiny", "base", "small"})


def _file_digest(path: Path, model: str) -> str:
    digest = hashlib.sha256(model.encode())
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class LocalWhisperClient:
    """Run a multilingual faster-whisper model locally without another credential."""

    def __init__(self, model: str = "small") -> None:
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"Unsupported local Whisper model: {model}")
        self.model_name = model
        self._model: Any | None = None

    def __enter__(self) -> LocalWhisperClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def _load(self) -> Any:
        if self._model is None:
            try:
                module = import_module("faster_whisper")
                model_class = module.__dict__["WhisperModel"]
                self._model = model_class(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=max(1, os.cpu_count() or 1),
                )
            except Exception as exc:
                raise ProviderError(
                    ProviderFailure(
                        provider="local_whisper",
                        category=ProviderErrorCategory.UNAVAILABLE,
                        message=f"Local Whisper model could not be loaded: {type(exc).__name__}",
                    )
                ) from exc
        return self._model

    def transcribe(self, prepared: PreparedAudio) -> TranscriptResult:
        """Transcribe normalized audio locally and retain segment timestamps."""
        model = self._load()
        try:
            raw_segments, info = model.transcribe(
                str(prepared.normalized_path),
                language="zh",
                beam_size=5,
                vad_filter=True,
            )
            segments = tuple(
                TranscriptSegment(
                    start_ms=max(0, round(float(segment.start) * 1000)),
                    end_ms=max(0, round(float(segment.end) * 1000)),
                    text=str(segment.text).strip(),
                )
                for segment in raw_segments
                if str(segment.text).strip()
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                ProviderFailure(
                    provider="local_whisper",
                    category=ProviderErrorCategory.UNKNOWN,
                    message=f"Local Whisper inference failed: {type(exc).__name__}",
                )
            ) from exc
        text = "\n".join(segment.text for segment in segments).strip()
        if not text:
            raise ProviderError(
                ProviderFailure(
                    provider="local_whisper",
                    category=ProviderErrorCategory.INVALID_INPUT,
                    message="Local Whisper returned no transcription text",
                )
            )
        language = str(getattr(info, "language", "zh") or "zh")
        return TranscriptResult(
            provider="local_whisper",
            provider_task_id=_file_digest(prepared.normalized_path, self.model_name),
            model=f"faster-whisper-{self.model_name}",
            language=language,
            duration_ms=round(prepared.probe.duration_seconds * 1000),
            text=text,
            segments=segments,
            timing_quality=TranscriptTimingQuality.EXACT,
        )
