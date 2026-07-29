"""FFmpeg-based bounded audio download, normalization, and silence-aware splitting."""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
MAX_REDIRECTS = 5
SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


class AudioPreparationError(RuntimeError):
    """Safe preprocessing failure without command stderr or remote response bodies."""


@dataclass(frozen=True)
class AudioProbe:
    """Relevant FFprobe metadata."""

    duration_seconds: float
    size_bytes: int
    format_name: str
    codec_name: str


@dataclass(frozen=True)
class SegmentPlan:
    """One overlap-aware logical segment."""

    start_seconds: float
    end_seconds: float
    overlap_seconds: float


@dataclass(frozen=True)
class AudioChunk:
    """Materialized audio chunk and its original timeline."""

    path: Path
    start_ms: int
    end_ms: int
    overlap_ms: int
    size_bytes: int


@dataclass(frozen=True)
class PreparedAudio:
    """Normalized episode audio and API-safe chunks."""

    normalized_path: Path
    probe: AudioProbe
    chunks: tuple[AudioChunk, ...]


def validate_public_audio_url(url: str) -> str:
    """Reject non-HTTPS, credential-bearing, localhost, and literal private IP URLs."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AudioPreparationError("Audio URL must use HTTPS and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise AudioPreparationError("Audio URL cannot contain credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise AudioPreparationError("Audio URL cannot target localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not address.is_global:
        raise AudioPreparationError("Audio URL cannot target a non-public IP address")
    return url


def download_audio(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> Path:
    """Stream a public audio URL with redirect and size limits."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    owns_client = client is None
    http = client or httpx.Client(timeout=60)
    current_url = validate_public_audio_url(url)
    try:
        for _redirect in range(MAX_REDIRECTS + 1):
            with http.stream("GET", current_url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise AudioPreparationError(
                            "Audio download redirect has no Location header"
                        )
                    current_url = validate_public_audio_url(urljoin(current_url, location))
                    continue
                if response.is_error:
                    raise AudioPreparationError(
                        f"Audio download failed with HTTP {response.status_code}"
                    )
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise AudioPreparationError("Audio download exceeds size limit")
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise AudioPreparationError("Audio download exceeds size limit")
                        output.write(chunk)
                return destination
        raise AudioPreparationError("Audio download exceeded redirect limit")
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise AudioPreparationError(
            f"Audio download transport failure: {type(exc).__name__}"
        ) from exc
    finally:
        if owns_client:
            http.close()


class AudioPreprocessor:
    """Run FFprobe and FFmpeg without a shell or persistent artifacts."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        self.ffmpeg = shutil.which(ffmpeg) or ffmpeg
        self.ffprobe = shutil.which(ffprobe) or ffprobe

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603 - argv only, never a shell
                list(command),
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError as exc:
            raise AudioPreparationError("FFmpeg/FFprobe is not installed on the runner") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioPreparationError("FFmpeg command timed out") from exc
        except subprocess.CalledProcessError as exc:
            raise AudioPreparationError(
                f"FFmpeg command failed with exit code {exc.returncode}"
            ) from exc

    def probe(self, path: Path) -> AudioProbe:
        """Read duration, size, container, and first audio codec."""
        result = self._run(
            (
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,format_name:stream=codec_name,codec_type",
                "-of",
                "json",
                str(path),
            )
        )
        try:
            payload = json.loads(result.stdout)
            format_value = payload["format"]
            streams = payload.get("streams", [])
            audio_stream = next(stream for stream in streams if stream.get("codec_type") == "audio")
            return AudioProbe(
                duration_seconds=float(format_value["duration"]),
                size_bytes=int(format_value["size"]),
                format_name=str(format_value["format_name"]),
                codec_name=str(audio_stream["codec_name"]),
            )
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise AudioPreparationError("FFprobe returned incomplete audio metadata") from exc

    def normalize(self, source: Path, destination: Path) -> Path:
        """Convert to mono 16 kHz 40 kbps MP3."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            (
                self.ffmpeg,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "40k",
                str(destination),
            )
        )
        return destination

    def silence_ends(self, path: Path) -> tuple[float, ...]:
        """Return silence-end timestamps suitable for nearby cut selection."""
        result = self._run(
            (
                self.ffmpeg,
                "-v",
                "info",
                "-i",
                str(path),
                "-af",
                "silencedetect=noise=-35dB:d=0.5",
                "-f",
                "null",
                "-",
            )
        )
        return tuple(float(value) for value in SILENCE_END.findall(result.stderr))

    def extract(
        self,
        source: Path,
        destination: Path,
        segment: SegmentPlan,
    ) -> Path:
        """Copy one normalized MP3 time range without re-encoding."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            (
                self.ffmpeg,
                "-y",
                "-v",
                "error",
                "-ss",
                f"{segment.start_seconds:.3f}",
                "-i",
                str(source),
                "-t",
                f"{segment.end_seconds - segment.start_seconds:.3f}",
                "-c",
                "copy",
                str(destination),
            )
        )
        return destination

    def prepare(self, source: Path, workdir: Path) -> PreparedAudio:
        """Normalize and materialize silence-aware API-safe chunks."""
        normalized = self.normalize(source, workdir / "normalized.mp3")
        probe = self.probe(normalized)
        silences = self.silence_ends(normalized) if probe.duration_seconds > 1800 else ()
        plans = plan_segments(probe.duration_seconds, silences)
        chunks: list[AudioChunk] = []
        for index, plan in enumerate(plans, start=1):
            path = self.extract(
                normalized,
                workdir / f"chunk-{index:03d}.mp3",
                plan,
            )
            size = path.stat().st_size
            if plan.end_seconds - plan.start_seconds > 3600 or size > 50 * 1024 * 1024:
                raise AudioPreparationError(
                    "Prepared chunk exceeds SiliconFlow 1-hour or 50MB limit"
                )
            chunks.append(
                AudioChunk(
                    path=path,
                    start_ms=round(plan.start_seconds * 1000),
                    end_ms=round(plan.end_seconds * 1000),
                    overlap_ms=round(plan.overlap_seconds * 1000),
                    size_bytes=size,
                )
            )
        return PreparedAudio(
            normalized_path=normalized,
            probe=probe,
            chunks=tuple(chunks),
        )


def plan_segments(
    duration_seconds: float,
    silence_ends: Sequence[float],
    *,
    minimum_seconds: float = 25 * 60,
    target_seconds: float = 28 * 60,
    maximum_seconds: float = 30 * 60,
    overlap_seconds: float = 3,
) -> tuple[SegmentPlan, ...]:
    """Choose silence cuts inside 25-30 minute windows and add overlap."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if not 0 <= overlap_seconds < minimum_seconds < target_seconds <= maximum_seconds:
        raise ValueError("invalid segment duration policy")
    boundaries: list[float] = []
    logical_start = 0.0
    candidates = sorted(value for value in silence_ends if 0 < value < duration_seconds)
    while duration_seconds - logical_start > maximum_seconds:
        lower = logical_start + minimum_seconds
        upper = logical_start + maximum_seconds
        nearby = [value for value in candidates if lower <= value <= upper]
        boundary = (
            min(nearby, key=lambda value: abs(value - (logical_start + target_seconds)))
            if nearby
            else logical_start + target_seconds
        )
        boundaries.append(boundary)
        logical_start = boundary
    boundaries.append(duration_seconds)

    segments: list[SegmentPlan] = []
    previous_boundary = 0.0
    for index, boundary in enumerate(boundaries):
        start = previous_boundary if index == 0 else previous_boundary - overlap_seconds
        segments.append(
            SegmentPlan(
                start_seconds=max(0.0, start),
                end_seconds=boundary,
                overlap_seconds=0.0 if index == 0 else overlap_seconds,
            )
        )
        previous_boundary = boundary
    return tuple(segments)
