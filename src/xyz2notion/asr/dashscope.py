"""Alibaba Cloud DashScope Paraformer recorded-audio ASR provider."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from xyz2notion.asr.audio import AudioPreparationError, validate_public_audio_url
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

DASHSCOPE_TRANSCRIPTION_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
)
DASHSCOPE_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
DEFAULT_MODEL = "paraformer-v1"
FREE_MODELS = frozenset({DEFAULT_MODEL})
SUCCEEDED_STATUSES = frozenset({"SUCCEEDED"})
RUNNING_STATUSES = frozenset({"PENDING", "RUNNING"})
FAILED_STATUSES = frozenset({"FAILED", "UNKNOWN"})


class DashScopeAPIError(RuntimeError):
    """Safe low-level DashScope failure without raw credential-bearing payloads."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(redact_text(message))
        self.status_code = status_code
        self.retryable = retryable
        self.code = code


def _json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DashScopeAPIError(
            "DashScope returned a non-JSON response",
            status_code=response.status_code,
        ) from exc
    if not isinstance(payload, Mapping):
        raise DashScopeAPIError(
            "DashScope returned an unexpected JSON shape",
            status_code=response.status_code,
        )
    return payload


def _status_category(status_code: int | None, code: str | None) -> ProviderErrorCategory:
    normalized = (code or "").lower()
    if status_code in {401, 403} or "invalidapikey" in normalized:
        return ProviderErrorCategory.AUTHENTICATION
    if status_code == 429 or "throttling" in normalized:
        return ProviderErrorCategory.RATE_LIMITED
    if "quota" in normalized or "allocation" in normalized:
        return ProviderErrorCategory.QUOTA_EXHAUSTED
    if status_code == 400:
        return ProviderErrorCategory.INVALID_INPUT
    if status_code in {500, 502, 503, 504}:
        return ProviderErrorCategory.UNAVAILABLE
    return ProviderErrorCategory.UNKNOWN


def _failure(
    category: ProviderErrorCategory,
    message: str,
    *,
    code: str | None = None,
) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="dashscope",
            category=category,
            message=message,
            code=code,
        )
    )


def _output(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise DashScopeAPIError("DashScope response has no output object")
    return output


def _extract_task_id(payload: Mapping[str, Any]) -> str:
    output = _output(payload)
    task_id = output.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise DashScopeAPIError("DashScope response has no task_id")
    return task_id.strip()


def _extract_result_url(payload: Mapping[str, Any]) -> str:
    output = _output(payload)
    status = output.get("task_status")
    if isinstance(status, str) and status in FAILED_STATUSES:
        code = output.get("code")
        message = output.get("message") or "DashScope transcription task failed"
        raise DashScopeAPIError(str(message), code=str(code) if code else status)
    results = output.get("results")
    if not isinstance(results, list) or not results:
        raise DashScopeAPIError("DashScope completed task has no results")
    first = results[0]
    if not isinstance(first, Mapping):
        raise DashScopeAPIError("DashScope task result has an unexpected shape")
    url = first.get("transcription_url")
    if not isinstance(url, str) or not url.strip():
        raise DashScopeAPIError("DashScope task result has no transcription_url")
    return url.strip()


def _int_ms(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value)))
        except ValueError:
            return default
    return default


def _sentence_text(sentence: Mapping[str, Any]) -> str:
    for key in ("text", "sentence", "content"):
        value = sentence.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    words = sentence.get("words")
    if isinstance(words, list):
        text = "".join(
            word.get("text", "")
            for word in words
            if isinstance(word, Mapping) and isinstance(word.get("text"), str)
        )
        if text.strip():
            return text.strip()
    return ""


def parse_transcription_result(
    payload: Mapping[str, Any],
    *,
    provider_task_id: str,
    model: str = DEFAULT_MODEL,
) -> TranscriptResult:
    """Parse DashScope result JSON into the provider-independent transcript contract."""
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        raise DashScopeAPIError("DashScope transcription JSON has no transcripts")

    texts: list[str] = []
    segments: list[TranscriptSegment] = []
    duration_ms = 0
    for transcript in transcripts:
        if not isinstance(transcript, Mapping):
            continue
        text = transcript.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
        duration_ms = max(
            duration_ms,
            _int_ms(transcript.get("content_duration_in_milliseconds")),
            _int_ms(transcript.get("duration")),
        )
        sentences = transcript.get("sentences")
        if isinstance(sentences, list):
            for sentence in sentences:
                if not isinstance(sentence, Mapping):
                    continue
                sentence_text = _sentence_text(sentence)
                if not sentence_text:
                    continue
                start = _int_ms(
                    sentence.get("begin_time", sentence.get("start_time", sentence.get("start")))
                )
                end = _int_ms(
                    sentence.get("end_time", sentence.get("end")),
                    default=start,
                )
                if end < start:
                    end = start
                duration_ms = max(duration_ms, end)
                speaker_value = sentence.get("speaker_id", sentence.get("speaker"))
                speaker = str(speaker_value) if speaker_value is not None else None
                segments.append(
                    TranscriptSegment(
                        start_ms=start,
                        end_ms=end,
                        text=sentence_text,
                        speaker=speaker,
                    )
                )
    if not texts and segments:
        texts.append("\n".join(segment.text for segment in segments))
    merged_text = "\n".join(texts).strip()
    if not merged_text:
        raise DashScopeAPIError("DashScope transcription JSON has no text")
    return TranscriptResult(
        provider="dashscope",
        provider_task_id=provider_task_id,
        model=model,
        duration_ms=duration_ms,
        text=merged_text,
        segments=tuple(segments),
        timing_quality=(
            TranscriptTimingQuality.EXACT if segments else TranscriptTimingQuality.UNKNOWN
        ),
    )


class DashScopeParaformerClient:
    """Asynchronous DashScope Paraformer client for public audio URLs."""

    _retryable_statuses = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        model: str = DEFAULT_MODEL,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        poll_attempts: int = 60,
        poll_interval_seconds: float = 10,
        timeout_seconds: float = 120,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            raise ValueError("DashScope API key cannot be empty")
        if model not in FREE_MODELS:
            raise ValueError("DashScope ASR model must be paraformer-v1")
        if max_retries < 0 or poll_attempts < 1 or poll_interval_seconds < 0:
            raise ValueError("DashScope retry and polling limits must be non-negative")
        validate_credential_destination(DASHSCOPE_TRANSCRIPTION_URL, CredentialKind.DASHSCOPE)
        self.model = model
        self.max_retries = max_retries
        self.poll_attempts = poll_attempts
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DashScopeParaformerClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        attach_auth: bool = True,
    ) -> Mapping[str, Any]:
        if attach_auth:
            validate_credential_destination(url, CredentialKind.DASHSCOPE)
        headers = self._headers if attach_auth else None
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.request(method, url, headers=headers, json=json_body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise DashScopeAPIError(
                        f"DashScope transport failure: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
            else:
                if response.status_code in self._retryable_statuses:
                    if attempt >= self.max_retries:
                        payload = _json(response)
                        raise DashScopeAPIError(
                            str(payload.get("message") or "DashScope request failed"),
                            status_code=response.status_code,
                            retryable=True,
                            code=str(payload.get("code")) if payload.get("code") else None,
                        )
                elif response.is_error:
                    payload = _json(response)
                    raise DashScopeAPIError(
                        str(payload.get("message") or "DashScope request failed"),
                        status_code=response.status_code,
                        code=str(payload.get("code")) if payload.get("code") else None,
                    )
                else:
                    return _json(response)
            self._sleep(min(30.0, 2.0**attempt))
        raise AssertionError("DashScope retry loop exhausted unexpectedly")

    def submit(self, audio_url: str) -> str:
        """Submit one public audio URL and return the async task id."""
        try:
            safe_audio_url = validate_public_audio_url(audio_url)
        except AudioPreparationError as exc:
            raise _failure(
                ProviderErrorCategory.INVALID_INPUT,
                "Audio URL is not usable by DashScope",
                code=type(exc).__name__,
            ) from exc
        payload = self._request_json(
            "POST",
            DASHSCOPE_TRANSCRIPTION_URL,
            json_body={
                "model": self.model,
                "input": {"file_urls": [safe_audio_url]},
                "parameters": {
                    "channel_id": [0],
                    "disfluency_removal_enabled": False,
                },
            },
        )
        try:
            return _extract_task_id(payload)
        except DashScopeAPIError as exc:
            raise _failure(ProviderErrorCategory.SCHEMA_CHANGED, str(exc), code=exc.code) from exc

    def wait_result_url(self, task_id: str) -> str:
        """Poll DashScope until the task succeeds or reaches a terminal failure."""
        task_url = DASHSCOPE_TASK_URL.format(task_id=task_id)
        for attempt in range(self.poll_attempts):
            # DashScope's recorded-speech API documents task-status polling as
            # POST with an empty body (the submission endpoint is also POST).
            payload = self._request_json("POST", task_url)
            try:
                output = _output(payload)
            except DashScopeAPIError as exc:
                raise _failure(
                    ProviderErrorCategory.SCHEMA_CHANGED, str(exc), code=exc.code
                ) from exc
            status = output.get("task_status")
            if status in SUCCEEDED_STATUSES:
                try:
                    return _extract_result_url(payload)
                except DashScopeAPIError as exc:
                    raise _failure(
                        ProviderErrorCategory.SCHEMA_CHANGED,
                        str(exc),
                        code=exc.code,
                    ) from exc
            if status in FAILED_STATUSES:
                code = output.get("code")
                message = str(output.get("message") or "DashScope transcription task failed")
                raise _failure(
                    _status_category(None, str(code) if code else None),
                    redact_text(message),
                    code=str(code) if code else str(status),
                )
            if attempt + 1 < self.poll_attempts:
                self._sleep(self.poll_interval_seconds)
        raise _failure(
            ProviderErrorCategory.TIMEOUT,
            "DashScope transcription task did not complete in time",
            code="poll_exhausted",
        )

    def fetch_transcript(self, url: str, *, task_id: str) -> TranscriptResult:
        """Fetch the public result JSON and parse transcript text plus sentence timing."""
        try:
            safe_url = validate_public_audio_url(url)
        except AudioPreparationError as exc:
            raise _failure(
                ProviderErrorCategory.SCHEMA_CHANGED,
                "DashScope returned an invalid transcription_url",
                code=type(exc).__name__,
            ) from exc
        try:
            payload = self._request_json("GET", safe_url, attach_auth=False)
            return parse_transcription_result(
                payload,
                provider_task_id=task_id,
                model=self.model,
            )
        except DashScopeAPIError as exc:
            raise _failure(ProviderErrorCategory.SCHEMA_CHANGED, str(exc), code=exc.code) from exc

    def transcribe_url(self, audio_url: str) -> TranscriptResult:
        """Submit, poll, fetch, and normalize one public audio URL."""
        try:
            task_id = self.submit(audio_url)
            result_url = self.wait_result_url(task_id)
            return self.fetch_transcript(result_url, task_id=task_id)
        except ProviderError:
            raise
        except DashScopeAPIError as exc:
            raise ProviderError(
                ProviderFailure(
                    provider="dashscope",
                    category=_status_category(exc.status_code, exc.code),
                    message=str(exc),
                    code=exc.code or (str(exc.status_code) if exc.status_code else None),
                )
            ) from exc
