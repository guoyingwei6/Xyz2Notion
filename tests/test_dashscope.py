import httpx
import pytest
from pydantic import SecretStr

from xyz2notion.asr.dashscope import (
    DASHSCOPE_TASK_URL,
    DASHSCOPE_TRANSCRIPTION_URL,
    DashScopeAPIError,
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
            assert request.method == "POST"
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
    with pytest.raises(ValueError, match="cannot be empty"):
        DashScopeParaformerClient("")
    with pytest.raises(ValueError, match="paraformer-v1"):
        DashScopeParaformerClient("key", model="paraformer-v2")
    with pytest.raises(ValueError, match="non-negative"):
        DashScopeParaformerClient("key", max_retries=-1)
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


def test_submit_rejects_private_audio_url_before_request() -> None:
    requests: list[httpx.Request] = []
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200)
            )
        ),
    )

    with pytest.raises(ProviderError) as caught:
        client.submit("http://127.0.0.1/audio.mp3")

    assert caught.value.failure.category is ProviderErrorCategory.INVALID_INPUT
    assert requests == []


def test_submit_schema_error_when_task_id_missing() -> None:
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"output": {}}))
        ),
    )

    with pytest.raises(ProviderError) as caught:
        client.submit("https://example.com/audio.mp3")

    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_wait_result_url_polls_running_then_succeeds() -> None:
    statuses = iter(
        [
            {"output": {"task_status": "PENDING"}},
            {
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {"transcription_url": ("https://dashscope.aliyuncs.com/result/task-3.json")}
                    ],
                }
            },
        ]
    )
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json=next(statuses))

    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        poll_interval_seconds=3,
        sleep=sleeps.append,
    )

    assert client.wait_result_url("task-3") == "https://dashscope.aliyuncs.com/result/task-3.json"
    assert sleeps == [3]


@pytest.mark.parametrize(
    ("status_code", "code", "category"),
    [
        (401, "InvalidApiKey", ProviderErrorCategory.AUTHENTICATION),
        (400, "BadRequest", ProviderErrorCategory.INVALID_INPUT),
        (503, "InternalError", ProviderErrorCategory.UNAVAILABLE),
    ],
)
def test_transcribe_url_maps_dashscope_status_categories(
    status_code: int,
    code: str,
    category: ProviderErrorCategory,
) -> None:
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    status_code,
                    json={"code": code, "message": "safe provider failure"},
                )
            )
        ),
        max_retries=0,
    )

    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")

    assert caught.value.failure.category is category


def test_wait_result_url_failed_task_maps_quota_category() -> None:
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "output": {
                            "task_status": "FAILED",
                            "code": "AllocationQuota.FreeTierOnly",
                            "message": "quota exhausted",
                        }
                    },
                )
            )
        ),
    )

    with pytest.raises(ProviderError) as caught:
        client.wait_result_url("task-quota")

    assert caught.value.failure.category is ProviderErrorCategory.QUOTA_EXHAUSTED


def test_wait_result_url_times_out_after_poll_limit() -> None:
    sleeps: list[float] = []
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"output": {"task_status": "RUNNING"}})
            )
        ),
        poll_attempts=2,
        poll_interval_seconds=1,
        sleep=sleeps.append,
    )

    with pytest.raises(ProviderError) as caught:
        client.wait_result_url("task-running")

    assert sleeps == [1]
    assert caught.value.failure.category is ProviderErrorCategory.TIMEOUT


def test_fetch_transcript_rejects_non_public_result_url() -> None:
    client = DashScopeParaformerClient("dashscope-fixture-secret")

    with pytest.raises(ProviderError) as caught:
        client.fetch_transcript("http://127.0.0.1/result.json", task_id="task-local")

    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"transcripts": []},
        {"transcripts": [{"sentences": [{"begin_time": True, "end_time": "bad"}]}]},
    ],
)
def test_parse_transcription_result_rejects_empty_text(payload: dict[str, object]) -> None:
    with pytest.raises(DashScopeAPIError):
        parse_transcription_result(payload, provider_task_id="task-empty")


def test_parse_transcription_result_accepts_word_segments_and_clamps_end() -> None:
    result = parse_transcription_result(
        {
            "transcripts": [
                {
                    "duration": "5.5",
                    "sentences": [
                        {
                            "start": 20,
                            "end": 10,
                            "words": [{"text": "你"}, {"text": "好"}],
                            "speaker": "A",
                        }
                    ],
                }
            ]
        },
        provider_task_id="task-words",
    )

    assert result.text == "你好"
    assert result.duration_ms == 20
    assert result.segments[0].end_ms == result.segments[0].start_ms
    assert result.segments[0].speaker == "A"


def test_secretstr_key_context_manager_and_owned_close() -> None:
    with DashScopeParaformerClient(
        SecretStr("dashscope-fixture-secret"),
        timeout_seconds=1,
    ) as client:
        assert client.model == "paraformer-v1"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=["unexpected"]),
    ],
)
def test_request_json_rejects_non_mapping_payload(response: httpx.Response) -> None:
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: response)),
    )

    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")

    assert caught.value.failure.category in {
        ProviderErrorCategory.UNKNOWN,
        ProviderErrorCategory.SCHEMA_CHANGED,
    }


def test_request_json_retries_transport_error_then_fails_safely() -> None:
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=1,
        sleep=sleeps.append,
    )

    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")

    assert sleeps == [1.0]
    assert caught.value.failure.category is ProviderErrorCategory.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        {"output": {"task_status": "SUCCEEDED", "results": []}},
        {"output": {"task_status": "SUCCEEDED", "results": ["bad"]}},
        {"output": {"task_status": "SUCCEEDED", "results": [{}]}},
        {
            "output": {
                "task_status": "FAILED",
                "code": "InvalidApiKey",
                "message": "auth failed",
            }
        },
    ],
)
def test_wait_result_url_rejects_bad_terminal_payloads(
    payload: dict[str, object],
) -> None:
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ),
    )

    with pytest.raises(ProviderError):
        client.wait_result_url("task-bad")


def test_fetch_transcript_maps_result_json_schema_error() -> None:
    client = DashScopeParaformerClient(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"transcripts": []})
            )
        ),
    )

    with pytest.raises(ProviderError) as caught:
        client.fetch_transcript(
            "https://dashscope.aliyuncs.com/result/task-empty.json",
            task_id="task-empty",
        )

    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED
