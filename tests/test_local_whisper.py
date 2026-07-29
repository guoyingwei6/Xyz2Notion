from pathlib import Path
from types import SimpleNamespace

import pytest

from xyz2notion.asr.audio import AudioProbe, PreparedAudio
from xyz2notion.asr.local_whisper import LocalWhisperClient
from xyz2notion.models import ProviderError, TranscriptTimingQuality


def prepared_audio(tmp_path: Path) -> PreparedAudio:
    normalized = tmp_path / "episode.mp3"
    normalized.write_bytes(b"normalized audio")
    return PreparedAudio(
        normalized_path=normalized,
        probe=AudioProbe(
            duration_seconds=12.5,
            size_bytes=normalized.stat().st_size,
            format_name="mp3",
            codec_name="mp3",
        ),
        chunks=(),
    )


class FakeModel:
    def transcribe(self, *_args: object, **_kwargs: object) -> tuple[object, object]:
        return (
            iter(
                (
                    SimpleNamespace(start=0.0, end=1.25, text=" 第一段 "),
                    SimpleNamespace(start=1.25, end=2.5, text="第二段"),
                )
            ),
            SimpleNamespace(language="zh"),
        )


def test_local_whisper_emits_exact_timestamped_contract(tmp_path: Path) -> None:
    client = LocalWhisperClient("small")
    client._model = FakeModel()
    result = client.transcribe(prepared_audio(tmp_path))
    assert result.provider == "local_whisper"
    assert result.model == "faster-whisper-small"
    assert result.duration_ms == 12_500
    assert result.text == "第一段\n第二段"
    assert [(segment.start_ms, segment.end_ms) for segment in result.segments] == [
        (0, 1_250),
        (1_250, 2_500),
    ]
    assert result.timing_quality is TranscriptTimingQuality.EXACT


def test_local_whisper_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        LocalWhisperClient("large")


def test_local_whisper_empty_result_is_safe_provider_failure(tmp_path: Path) -> None:
    class EmptyModel:
        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[object, object]:
            return iter(()), SimpleNamespace(language="zh")

    client = LocalWhisperClient("base")
    client._model = EmptyModel()
    with pytest.raises(ProviderError, match="no transcription"):
        client.transcribe(prepared_audio(tmp_path))
