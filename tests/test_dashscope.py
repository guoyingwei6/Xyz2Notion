import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from xyz2notion.asr.dashscope import (
    DASHSCOPE_TASK_URL,
    DASHSCOPE_TRANSCRIPTION_URL,
    AsrFreeTierPausedError,
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


def confirmed_client(api_key: str | SecretStr, **kwargs: Any) -> DashScopeParaformerClient:
    """Protocol fixtures explicitly attest free-only protection; never use a real key."""
    return DashScopeParaformerClient(
        api_key,
        confirmed_free_tier_models=kwargs.get("models", (kwargs.get("model", "paraformer-v1"),)),
        **kwargs,
    )


def test_unconfirmed_model_makes_no_http_request() -> None:
    def forbidden(_request: httpx.Request) -> httpx.Response:
        pytest.fail("An unconfirmed model must never reach the network")

    with DashScopeParaformerClient(
        "fixture-key", client=httpx.Client(transport=httpx.MockTransport(forbidden))
    ) as client:
        with pytest.raises(AsrFreeTierPausedError, match="no DashScope model"):
            client.ensure_free_tier()
        with pytest.raises(ProviderError) as caught:
            client.submit_with_fallback("https://example.com/audio.mp3")
        assert caught.value.failure.code == "free_tier_unconfirmed"

    with pytest.raises(ValueError, match="confirmations"):
        DashScopeParaformerClient(
            "fixture-key", confirmed_free_tier_models=("not-a-configured-model",)
        )


@pytest.mark.parametrize(
    "model",
    [
        "fun-asr",
        "fun-asr-mtl",
        "qwen-audio-3.0-asr-flash-filetrans",
        "qwen3-asr-flash-filetrans",
    ],
)
def test_new_recorded_models_submit_poll_and_parse(model: str) -> None:
    calls: list[str] = []
    audio = "https://example.com/audio.mp3"
    result_url = "https://example.com/result.json"

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["model"] == model
            if model == "qwen3-asr-flash-filetrans":
                assert body["input"] == {"file_url": audio}
                assert body["parameters"] == {}
            else:
                assert body["input"] == {"file_urls": [audio]}
                assert body["parameters"] == {"channel_id": [0]}
            return httpx.Response(200, json={"output": {"task_id": "new-task"}})
        if str(request.url) == DASHSCOPE_TASK_URL.format(task_id="new-task"):
            result = {"transcription_url": result_url}
            output = (
                {"result": result}
                if model == "qwen3-asr-flash-filetrans"
                else {"results": [result]}
            )
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", **output}})
        assert str(request.url) == result_url
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {
                        "text": "transcript",
                        "sentences": [{"begin_time": 0, "end_time": 5000, "text": "transcript"}],
                    }
                ]
            },
        )

    with confirmed_client(
        "fixture-key", model=model, client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        result = client.transcribe_url(audio)
    assert calls == ["POST", "GET", "GET"]
    assert result.model == model
    assert result.text == "transcript"
    assert result.duration_ms == 5000
    assert result.timing_quality.value == "exact_timestamps"


def test_free_quota_fallback_skips_unconfirmed_models() -> None:
    submitted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        submitted.append(model)
        if model == "paraformer-v2":
            return httpx.Response(403, json={"code": "AllocationQuota.FreeTierOnly"})
        assert model == "fun-asr-mtl"
        return httpx.Response(200, json={"output": {"task_id": "last-task"}})

    with DashScopeParaformerClient(
        "fixture-key",
        models=("paraformer-v2", "fun-asr", "fun-asr-mtl"),
        confirmed_free_tier_models=("paraformer-v2", "fun-asr-mtl"),
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        client.ensure_free_tier()
        assert client.submit_with_fallback("https://example.com/audio.mp3") == (
            "last-task",
            "fun-asr-mtl",
        )
    assert submitted == ["paraformer-v2", "fun-asr-mtl"]


def test_failed_subtask_keeps_sensitive_error_visible() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "subtask_status": "FAILED",
                            "code": "DataInspectionFailed",
                            "message": "Content inspection rejected the audio",
                        }
                    ],
                }
            },
        )

    with (
        confirmed_client(
            "fixture-key", client=httpx.Client(transport=httpx.MockTransport(handle))
        ) as client,
        pytest.raises(ProviderError) as caught,
    ):
        client.wait_result_url("task")
    assert caught.value.failure.code == "DataInspectionFailed"


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
            assert request.method == "GET"
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

    client = confirmed_client(
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

    client = confirmed_client(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=0,
    )
    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")
    assert caught.value.failure.category is ProviderErrorCategory.RATE_LIMITED
    assert caught.value.failure.retryable is True
    assert "fixture-secret" not in str(caught.value)


def test_dashscope_model_allowlist_and_unsafe_hosts() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        confirmed_client("")
    client = confirmed_client(
        "key",
        models=("paraformer-v1", "paraformer-v2", "paraformer-mtl-v1"),
    )
    assert client.models == ("paraformer-v1", "paraformer-v2", "paraformer-mtl-v1")
    with pytest.raises(ValueError, match="safe allowlist"):
        confirmed_client("key", model="not-a-paraformer")
    with pytest.raises(ValueError, match="cannot be empty"):
        confirmed_client("key", models=())
    with pytest.raises(ValueError, match="cannot be empty"):
        confirmed_client("key", models=(" ",))
    with pytest.raises(ValueError, match="duplicates"):
        confirmed_client("key", models=("paraformer-v1", "paraformer-v1"))
    with pytest.raises(ValueError, match="non-negative"):
        confirmed_client("key", max_retries=-1)
    with pytest.raises(UnsafeCredentialDestinationError):
        validate_credential_destination(
            "https://evil.example/api/v1/services/audio/asr/transcription",
            CredentialKind.DASHSCOPE,
        )


def test_dashscope_model_quota_falls_back_before_task_submission() -> None:
    submitted_models: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DASHSCOPE_TRANSCRIPTION_URL:
            payload = json.loads(request.content)
            model = str(payload["model"])
            submitted_models.append(model)
            if model == "paraformer-v1":
                return httpx.Response(
                    403,
                    json={
                        "code": "AllocationQuota.FreeTierOnly",
                        "message": "free quota exhausted",
                    },
                )
            assert model == "paraformer-v2"
            assert payload["parameters"]["timestamp_alignment_enabled"] is True
            return httpx.Response(200, json={"output": {"task_id": "task-v2"}})
        if str(request.url) == DASHSCOPE_TASK_URL.format(task_id="task-v2"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "transcription_url": (
                                    "https://dashscope.aliyuncs.com/result/task-v2.json"
                                )
                            }
                        ],
                    }
                },
            )
        if str(request.url) == "https://dashscope.aliyuncs.com/result/task-v2.json":
            return httpx.Response(
                200,
                json={"transcripts": [{"text": "v2 文本", "sentences": []}]},
            )
        raise AssertionError(str(request.url))

    client = confirmed_client(
        "dashscope-fixture-secret",
        models=("paraformer-v1", "paraformer-v2", "paraformer-mtl-v1"),
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=0,
        sleep=lambda _seconds: None,
    )

    result = client.transcribe_url("https://example.com/audio.mp3")

    assert submitted_models == ["paraformer-v1", "paraformer-v2"]
    assert result.model == "paraformer-v2"


def test_dashscope_does_not_create_second_task_after_submission_failure() -> None:
    submitted_models: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DASHSCOPE_TRANSCRIPTION_URL:
            submitted_models.append(str(json.loads(request.content)["model"]))
            return httpx.Response(200, json={"output": {"task_id": "task-running"}})
        if str(request.url) == DASHSCOPE_TASK_URL.format(task_id="task-running"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "FAILED",
                        "code": "AllocationQuota.FreeTierOnly",
                        "message": "quota exhausted after task creation",
                    }
                },
            )
        raise AssertionError(str(request.url))

    client = confirmed_client(
        "dashscope-fixture-secret",
        models=("paraformer-v1", "paraformer-v2"),
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")

    assert caught.value.failure.category is ProviderErrorCategory.QUOTA_EXHAUSTED
    assert submitted_models == ["paraformer-v1"]


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
    client = confirmed_client(
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
    client = confirmed_client(
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
        assert request.method == "GET"
        return httpx.Response(200, json=next(statuses))

    client = confirmed_client(
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
        (503, "InternalError", ProviderErrorCategory.UNKNOWN),
    ],
)
def test_transcribe_url_maps_dashscope_status_categories(
    status_code: int,
    code: str,
    category: ProviderErrorCategory,
) -> None:
    client = confirmed_client(
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
    client = confirmed_client(
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
    client = confirmed_client(
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
    client = confirmed_client("dashscope-fixture-secret")

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
    with confirmed_client(
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
    client = confirmed_client(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: response)),
    )

    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")

    assert caught.value.failure.category in {
        ProviderErrorCategory.UNKNOWN,
        ProviderErrorCategory.SCHEMA_CHANGED,
    }


def test_submission_transport_error_is_not_replayed() -> None:
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = confirmed_client(
        "dashscope-fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=1,
        sleep=sleeps.append,
    )

    with pytest.raises(ProviderError) as caught:
        client.transcribe_url("https://example.com/audio.mp3")

    assert sleeps == []
    assert caught.value.failure.category is ProviderErrorCategory.UNKNOWN


@pytest.mark.parametrize("body", ["not json", "[]", "{}"])
def test_invalid_submission_response_requires_audit(body: str) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=body)

    client = confirmed_client(
        "fixture-key",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        models=("paraformer-v1", "paraformer-v2"),
    )
    with pytest.raises(ProviderError) as error:
        client.submit_with_fallback("https://example.com/audio.mp3")
    assert error.value.failure.code == "ambiguous_submission"
    assert len(requests) == 1


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
    client = confirmed_client(
        "dashscope-fixture-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ),
    )

    with pytest.raises(ProviderError):
        client.wait_result_url("task-bad")


def test_fetch_transcript_maps_result_json_schema_error() -> None:
    client = confirmed_client(
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
