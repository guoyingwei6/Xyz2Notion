"""SiliconFlow free ASR provider with model fallback and coarse chunk timestamps."""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
from pydantic import SecretStr

from xyz2notion.asr.audio import AudioChunk, PreparedAudio
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)
from xyz2notion.security import (
    CredentialKind,
    redact_text,
    validate_credential_destination,
)

SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_MODELS = (
    "FunAudioLLM/SenseVoiceSmall",
    "TeleAI/TeleSpeechASR",
)
FREE_MODELS = frozenset(DEFAULT_MODELS)
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_DURATION_MS = 60 * 60 * 1000


class SiliconFlowAPIError(RuntimeError):
    """Safe low-level failure used for model fallback decisions."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        model_unavailable: bool = False,
    ) -> None:
        super().__init__(redact_text(message))
        self.status_code = status_code
        self.retryable = retryable
        self.model_unavailable = model_unavailable


def _deduplicate_overlap(previous: str, current: str, limit: int = 500) -> str:
    """Remove the longest exact suffix/prefix overlap, including Chinese text."""
    maximum = min(len(previous), len(current), limit)
    for size in range(maximum, 5, -1):
        if previous[-size:] == current[:size]:
            return current[size:].lstrip()
    return current


def merge_chunk_texts(texts: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """Merge chunk results and return the deduplicated text for each segment."""
    merged: list[str] = []
    segment_texts: list[str] = []
    for raw in texts:
        text = raw.strip()
        if not text:
            segment_texts.append("")
            continue
        deduplicated = _deduplicate_overlap(merged[-1], text) if merged else text
        if deduplicated:
            merged.append(deduplicated)
        segment_texts.append(deduplicated)
    return "\n".join(merged), tuple(segment_texts)


class SiliconFlowClient:
    """Official multipart transcription client for the two free ASR models."""

    _retryable_statuses = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        models: Sequence[str] = DEFAULT_MODELS,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 180,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            raise ValueError("SiliconFlow API key cannot be empty")
        normalized_models = tuple(model.strip() for model in models if model.strip())
        if not normalized_models:
            raise ValueError("at least one SiliconFlow model is required")
        if unknown := set(normalized_models) - FREE_MODELS:
            raise ValueError(
                f"SiliconFlow ASR models outside the free allowlist are not allowed: "
                f"{', '.join(sorted(unknown))}"
            )
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        validate_credential_destination(SILICONFLOW_API_URL, CredentialKind.SILICONFLOW)
        self.models = normalized_models
        self.max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._headers = {"Authorization": f"Bearer {key}"}
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SiliconFlowClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return max(0.0, float(response.headers["Retry-After"]))
            except ValueError:
                pass
        return min(30.0, 2.0**attempt) + self._jitter()

    def _transcribe_model(self, path: Path, model: str) -> str:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SiliconFlowAPIError("SiliconFlow audio file exceeds 50MB")
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                with path.open("rb") as audio:
                    response = self._client.post(
                        SILICONFLOW_API_URL,
                        headers=self._headers,
                        files={
                            "file": (path.name, audio, "audio/mpeg"),
                            "model": (None, model),
                        },
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise SiliconFlowAPIError(
                        f"SiliconFlow transport failure: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
            else:
                if response.status_code == 404:
                    raise SiliconFlowAPIError(
                        f"SiliconFlow model unavailable: {model}",
                        status_code=404,
                        model_unavailable=True,
                    )
                if response.status_code in self._retryable_statuses:
                    if attempt >= self.max_retries:
                        raise SiliconFlowAPIError(
                            f"SiliconFlow request failed with HTTP {response.status_code}",
                            status_code=response.status_code,
                            retryable=True,
                        )
                elif response.is_error:
                    raise SiliconFlowAPIError(
                        f"SiliconFlow request failed with HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise SiliconFlowAPIError(
                            "SiliconFlow returned a non-JSON response"
                        ) from exc
                    text = payload.get("text") if isinstance(payload, dict) else None
                    if not isinstance(text, str) or not text.strip():
                        raise SiliconFlowAPIError("SiliconFlow returned no transcription text")
                    return text.strip()
            self._sleep(self._delay(response, attempt))
        raise AssertionError("SiliconFlow retry loop exhausted unexpectedly")

    def transcribe_chunk(
        self,
        chunk: AudioChunk,
        *,
        models: Sequence[str] | None = None,
    ) -> tuple[str, str]:
        """Transcribe one chunk, switching only when a model is unavailable."""
        if chunk.end_ms - chunk.start_ms > MAX_DURATION_MS:
            raise SiliconFlowAPIError("SiliconFlow audio chunk exceeds 1 hour")
        candidates = tuple(models or self.models)
        last_unavailable: SiliconFlowAPIError | None = None
        for model in candidates:
            try:
                return self._transcribe_model(chunk.path, model), model
            except SiliconFlowAPIError as exc:
                if not exc.model_unavailable:
                    raise
                last_unavailable = exc
        raise last_unavailable or SiliconFlowAPIError("No SiliconFlow ASR model is available")

    def transcribe(self, prepared: PreparedAudio) -> TranscriptResult:
        """Transcribe all chunks and emit a provider-independent result."""
        texts: list[str] = []
        used_models: list[str] = []
        active_models = self.models
        try:
            for chunk in prepared.chunks:
                text, model = self.transcribe_chunk(chunk, models=active_models)
                texts.append(text)
                used_models.append(model)
                model_index = active_models.index(model)
                active_models = active_models[model_index:]
        except SiliconFlowAPIError as exc:
            category = (
                ProviderErrorCategory.RATE_LIMITED
                if exc.status_code == 429
                else ProviderErrorCategory.UNAVAILABLE
                if exc.model_unavailable or exc.status_code in self._retryable_statuses
                else ProviderErrorCategory.AUTHENTICATION
                if exc.status_code in {401, 403}
                else ProviderErrorCategory.INVALID_INPUT
                if exc.status_code == 400
                else ProviderErrorCategory.NETWORK
                if exc.retryable
                else ProviderErrorCategory.UNKNOWN
            )
            raise ProviderError(
                ProviderFailure(
                    provider="siliconflow",
                    category=category,
                    message=str(exc),
                    code=str(exc.status_code) if exc.status_code else None,
                )
            ) from exc

        merged, segment_texts = merge_chunk_texts(texts)
        segments = tuple(
            TranscriptSegment(
                start_ms=chunk.start_ms + chunk.overlap_ms,
                end_ms=chunk.end_ms,
                text=text,
            )
            for chunk, text in zip(prepared.chunks, segment_texts, strict=True)
            if text
        )
        digest = hashlib.sha256()
        for chunk in prepared.chunks:
            digest.update(chunk.path.read_bytes())
        digest.update(",".join(used_models).encode())
        return TranscriptResult(
            provider="siliconflow",
            provider_task_id=digest.hexdigest(),
            model=" -> ".join(dict.fromkeys(used_models)),
            duration_ms=round(prepared.probe.duration_seconds * 1000),
            text=merged,
            segments=segments,
            timing_quality=TranscriptTimingQuality.COARSE,
        )
