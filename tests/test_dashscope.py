import httpx
import pytest

from xyz2notion.asr.dashscope import (
    DASHSCOPE_TASK_URL,
    DASHSCOPE_TRANSCRIPTION_URL,
    DashScopeParaformerClient,
    parse_transcription_result,
)
from xyz2notion.models import ProviderError, ProviderErrorCategory
from xyz2notion.security import (
    CredentialKind,
    UnsafeCredentialDestinationError,
    validate_credential_destination,
)


def test_submit_poll_fetch_and_parse_paraformer_result() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == DASHSCOPE_TRANSCRIPTION_URL:
            assert request.headers["Authorization"] == "Bearer dashscope-fixture-secret"
            assert request.headers["X-DashScope-Async"] == "enable"
            body = request.content.decode()
            assert "paraformer-v1" in body
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if str(request.url) == DASHSCOPE_TASK_URL.format(task_id="task-1"):
            assert request.headers["Authorization"] == "Bearer dashscope-fixture-secret"
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "transcription_url": (
                                    "https://dashscope.aliyuncs.com/result/task-1.json"
                                )
                            }
                        ],
                    }
                },
            )
        if str(request.url) == "https://dashscope.aliyuncs.com/result/task-1.json":
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "transcripts": [
                        {
                            "content_duration_in_milliseconds": 1200,
                            "text": "全文",
                            "sentences": [
                                {
                                    "begin_time": 0,
                                    "end_time": 1200,
                                    "text": "全文",
                                    "speaker_id": 0,
                                }
                            ],
                        }
                    ]
                },
            )
        raise AssertionError(str(request.url))

    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        sleep=lambda _seconds: None,
    )
    result = client.transcribe_url("https://example.com/audio.mp3")

    assert calls == [
        DASHSCOPE_TRANSCRIPTION_URL,
        DASHSCOPE_TASK_URL.format(task_id="task-1"),
        "https://dashscope.aliyuncs.com/result/task-1.json",
    ]
    assert result.provider == "dashscope"
    assert result.provider_task_id == "task-1"
    assert result.model == "paraformer-v1"
    assert result.text == "全文"
    assert result.timing_quality.value == "exact_timestamps"
    assert result.segments[0].speaker == "0"


def test_dashscope_failures_are_safe_and_categorized() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "code": "Throttling",
                "message": "Authorization: Bearer dashscope-fixture-secret",
            },
        )

    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=0,
    )
    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")
    assert caught.value.failure.category is ProviderErrorCategory.RATE_LIMITED
    assert caught.value.failure.retryable is True
    assert "fixture-secret" not in str(caught.value)


def test_dashscope_rejects_non_free_model_and_unsafe_hosts() -> None:
    with pytest.raises(ValueError, match="paraformer-v1"):
        DashScopeParaformerClient("key", model="paraformer-v2")
    with pytest.raises(UnsafeCredentialDestinationError):
        validate_credential_destination(
            "https://evil.example/api/v1/services/audio/asr/transcription",
            CredentialKind.DASHSCOPE,
        )


def test_parse_transcription_result_accepts_sentence_only_payload() -> None:
    result = parse_transcription_result(
        {
            "transcripts": [
                {
                    "sentences": [
                        {"begin_time": "10", "end_time": "20", "text": "一句话"},
                    ]
                }
            ]
        },
        provider_task_id="task-2",
    )

    assert result.text == "一句话"
    assert result.duration_ms == 20
