import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from xyz2notion.asr.audio import (
    AudioChunk,
    AudioPreparationError,
    AudioPreprocessor,
    AudioProbe,
    PreparedAudio,
    download_audio,
    plan_segments,
    validate_public_audio_url,
)
from xyz2notion.asr.pipeline import transcribe_siliconflow_episode
from xyz2notion.models import TranscriptResult


def test_segment_plan_prefers_silence_inside_25_to_30_minute_window() -> None:
    segments = plan_segments(4000, (100, 1700, 3400, 3900))
    assert [(item.start_seconds, item.end_seconds) for item in segments] == [
        (0, 1700),
        (1697, 3400),
        (3397, 4000),
    ]
    assert [item.overlap_seconds for item in segments] == [0, 3, 3]
    assert all(item.end_seconds - item.start_seconds < 1805 for item in segments)


def test_segment_plan_uses_target_when_no_silence_is_available() -> None:
    segments = plan_segments(3700, ())
    assert [item.end_seconds for item in segments] == [1680, 3360, 3700]
    with pytest.raises(ValueError, match="positive"):
        plan_segments(0, ())
    with pytest.raises(ValueError, match="policy"):
        plan_segments(10, (), overlap_seconds=1600)


def test_silence_detector_parses_ffmpeg_timestamps(tmp_path: Path) -> None:
    class FakePreprocessor(AudioPreprocessor):
        def _run(
            self,
            command: list[str] | tuple[str, ...],
        ) -> subprocess.CompletedProcess[str]:
            assert "silencedetect=noise=-35dB:d=0.5" in command
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="",
                stderr=(
                    "[silencedetect] silence_end: 1500.25 | silence_duration: 1\n"
                    "[silencedetect] silence_end: 1701.5 | silence_duration: 2\n"
                ),
            )

    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    assert FakePreprocessor().silence_ends(path) == (1500.25, 1701.5)


@pytest.mark.parametrize(
    "url",
    (
        "http://cdn.example/audio.mp3",
        "https://localhost/audio.mp3",
        "https://127.0.0.1/audio.mp3",
        "https://user:password@cdn.example/audio.mp3",
    ),
)
def test_audio_url_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(AudioPreparationError):
        validate_public_audio_url(url)
    assert validate_public_audio_url("https://cdn.example/audio.mp3").startswith("https://")


def test_download_streams_redirected_audio_with_size_limit(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example":
            return httpx.Response(
                302,
                headers={"Location": "https://media.example/audio.mp3"},
            )
        return httpx.Response(
            200,
            headers={"Content-Length": "6"},
            content=b"audio!",
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        path = download_audio(
            "https://cdn.example/source",
            tmp_path / "audio.bin",
            client=http,
            max_bytes=10,
        )
    assert path.read_bytes() == b"audio!"


def test_download_rejects_declared_oversize(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Length": "100"},
            content=b"x",
        )
    )
    with (
        httpx.Client(transport=transport) as http,
        pytest.raises(AudioPreparationError, match="size limit"),
    ):
        download_audio(
            "https://cdn.example/audio",
            tmp_path / "audio",
            client=http,
            max_bytes=10,
        )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg is not installed",
)
def test_ffmpeg_probe_normalize_and_prepare_short_audio(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603 - test invokes a resolved local executable
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            str(source),
        ],
        check=True,
    )
    prepared = AudioPreprocessor().prepare(source, tmp_path / "prepared")
    assert prepared.probe.codec_name == "mp3"
    assert 1.9 <= prepared.probe.duration_seconds <= 2.1
    assert len(prepared.chunks) == 1
    assert prepared.chunks[0].size_bytes < 50 * 1024 * 1024


def test_prepare_is_covered_without_runner_ffmpeg(tmp_path: Path) -> None:
    """Keep the orchestration contract covered on minimal GitHub runners."""

    class FakePreprocessor(AudioPreprocessor):
        def normalize(self, source: Path, destination: Path) -> Path:
            assert source.read_bytes() == b"source"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"normalized")
            return destination

        def probe(self, path: Path) -> AudioProbe:
            assert path.read_bytes() == b"normalized"
            return AudioProbe(3700, path.stat().st_size, "mp3", "mp3")

        def silence_ends(self, path: Path) -> tuple[float, ...]:
            assert path.exists()
            return (1700, 3400)

        def extract(self, source: Path, destination: Path, segment: object) -> Path:
            assert source.exists()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"chunk")
            return destination

    source = tmp_path / "source.audio"
    source.write_bytes(b"source")
    prepared = FakePreprocessor().prepare(source, tmp_path / "prepared")

    assert prepared.probe.duration_seconds == 3700
    assert len(prepared.chunks) == 3
    assert [chunk.start_ms for chunk in prepared.chunks] == [0, 1_697_000, 3_397_000]
    assert all(chunk.path.is_file() for chunk in prepared.chunks)


def test_episode_pipeline_removes_temporary_audio_after_return() -> None:
    observed_paths: list[Path] = []

    class FakePreprocessor:
        def prepare(self, source: Path, workdir: Path) -> PreparedAudio:
            assert source.read_bytes() == b"audio"
            workdir.mkdir(parents=True)
            chunk = workdir / "chunk.mp3"
            chunk.write_bytes(b"prepared")
            observed_paths.extend((source, chunk))
            return PreparedAudio(
                normalized_path=chunk,
                probe=AudioProbe(1, 8, "mp3", "mp3"),
                chunks=(AudioChunk(chunk, 0, 1000, 0, 8),),
            )

    class FakeProvider:
        def transcribe(self, prepared: PreparedAudio) -> TranscriptResult:
            assert prepared.chunks[0].path.exists()
            return TranscriptResult(
                provider="siliconflow",
                provider_task_id="task",
                model="model",
                duration_ms=1000,
                text="transcript",
            )

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"audio"))
    with httpx.Client(transport=transport) as http:
        result = transcribe_siliconflow_episode(
            "https://cdn.example/audio",
            FakeProvider(),
            preprocessor=FakePreprocessor(),
            http_client=http,
        )
    assert result.text == "transcript"
    assert observed_paths
    assert all(not path.exists() for path in observed_paths)
