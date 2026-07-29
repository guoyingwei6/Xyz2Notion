from pathlib import Path

import httpx
import pytest

from xyz2notion.asr.audio import AudioChunk, AudioProbe, PreparedAudio
from xyz2notion.asr.siliconflow import (
    SiliconFlowAPIError,
    SiliconFlowClient,
    merge_chunk_texts,
)
from xyz2notion.models import ProviderError, ProviderErrorCategory
from xyz2notion.security import (
    CredentialKind,
    UnsafeCredentialDestinationError,
    validate_credential_destination,
)


def prepared_audio(tmp_path: Path) -> PreparedAudio:
    paths = []
    for index, content in enumerate((b"audio-one", b"audio-two"), start=1):
        path = tmp_path / f"chunk-{index}.mp3"
        path.write_bytes(content)
        paths.append(path)
    return PreparedAudio(
        normalized_path=paths[0],
        probe=AudioProbe(
            duration_seconds=100,
            size_bytes=18,
            format_name="mp3",
            codec_name="mp3",
        ),
        chunks=(
            AudioChunk(
                path=paths[0],
                start_ms=0,
                end_ms=60_000,
                overlap_ms=0,
                size_bytes=9,
            ),
            AudioChunk(
                path=paths[1],
                start_ms=57_000,
                end_ms=100_000,
                overlap_ms=3_000,
                size_bytes=9,
            ),
        ),
    )


def client_for(
    handler: httpx.MockTransport,
    *,
    sleeps: list[float] | None = None,
    max_retries: int = 2,
) -> SiliconFlowClient:
    return SiliconFlowClient(
        "siliconflow-fixture-secret",
        client=httpx.Client(transport=handler),
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda: 0,
        max_retries=max_retries,
    )


def multipart_model(request: httpx.Request) -> str:
    content = request.content.decode(errors="ignore")
    for model in (
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
    ):
        if model in content:
            return model
    raise AssertionError(content)


def test_model_unavailable_switches_once_and_coarse_segments_merge_overlap(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer siliconflow-fixture-secret"
        model = multipart_model(request)
        calls.append(model)
        if model == "FunAudioLLM/SenseVoiceSmall":
            return httpx.Response(404, text="model removed")
        text = "这是第一段重复内容ABCDEF" if len(calls) == 2 else "重复内容ABCDEF这是第二段"
        return httpx.Response(200, json={"text": text})

    result = client_for(httpx.MockTransport(handle)).transcribe(prepared_audio(tmp_path))
    assert calls == [
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
        "TeleAI/TeleSpeechASR",
    ]
    assert result.model == "TeleAI/TeleSpeechASR"
    assert result.timing_quality.value == "coarse_timestamps"
    assert result.segments[1].start_ms == 60_000
    assert result.text.count("重复内容ABCDEF") == 1
    assert len(result.provider_task_id) == 64


def test_rate_limit_retries_with_retry_after(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"text": "transcript"})

    client = client_for(httpx.MockTransport(handle), sleeps=sleeps)
    chunk = prepared_audio(tmp_path).chunks[0]
    assert client.transcribe_chunk(chunk)[0] == "transcript"
    assert sleeps == [2.0]


def test_safe_provider_error_is_retryable_and_does_not_leak_key(tmp_path: Path) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "message": "Authorization: Bearer siliconflow-fixture-secret",
            },
        )

    client = client_for(httpx.MockTransport(handle), max_retries=0)
    with pytest.raises(ProviderError) as caught:
        client.transcribe(prepared_audio(tmp_path))
    assert caught.value.failure.category is ProviderErrorCategory.UNAVAILABLE
    assert caught.value.failure.retryable is True
    assert "fixture-secret" not in str(caught.value)


def test_merge_chunk_texts_handles_empty_and_nonmatching_text() -> None:
    merged, segments = merge_chunk_texts([" first ", "", "second"])
    assert merged == "first\nsecond"
    assert segments == ("first", "", "second")


def test_client_and_chunk_limits_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        SiliconFlowClient("")
    with pytest.raises(ValueError, match="at least one"):
        SiliconFlowClient("key", models=())
    with pytest.raises(ValueError, match="negative"):
        SiliconFlowClient("key", max_retries=-1)
    with pytest.raises(UnsafeCredentialDestinationError):
        validate_credential_destination(
            "https://evil.example/v1/audio/transcriptions",
            CredentialKind.SILICONFLOW,
        )

    audio = prepared_audio(tmp_path)
    too_long = AudioChunk(
        path=audio.chunks[0].path,
        start_ms=0,
        end_ms=3_600_001,
        overlap_ms=0,
        size_bytes=9,
    )
    with pytest.raises(SiliconFlowAPIError, match="exceeds 1 hour"):
        client_for(
            httpx.MockTransport(lambda _request: httpx.Response(200, json={"text": "unused"}))
        ).transcribe_chunk(too_long)


def test_non_json_or_empty_transcription_is_rejected(tmp_path: Path) -> None:
    responses = iter(
        (
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json={"text": ""}),
        )
    )
    client = client_for(httpx.MockTransport(lambda _request: next(responses)))
    chunk = prepared_audio(tmp_path).chunks[0]
    with pytest.raises(SiliconFlowAPIError, match="non-JSON"):
        client.transcribe_chunk(chunk)
    with pytest.raises(SiliconFlowAPIError, match="no transcription"):
        client.transcribe_chunk(chunk)
