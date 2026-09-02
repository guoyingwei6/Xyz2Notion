import json
from collections.abc import Callable

import httpx
import pytest

from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.enrichment.siliconflow import (
    SILICONFLOW_CHAT_URL,
    CompletionUsage,
    SiliconFlowSummaryClient,
)
from xyz2notion.models import ProviderError, ProviderErrorCategory

API_KEY = "siliconflow-fixture-secret"


def payload(summary: str = "摘要") -> dict[str, object]:
    return {
        "summary": summary,
        "chapters": [{"start_ms": 0, "title": "开场", "summary": ""}],
        "highlights": ["观点"],
        "quotes": ["原文"],
        "terms": ["术语"],
        "people": ["人物"],
        "questions": ["问题"],
        "mindmap": {"node_id": "root", "title": "主题", "children": []},
    }


def completion(
    content: str,
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
            },
        },
    )


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    models: tuple[str, ...] = ("Qwen/Qwen3-8B",),
    max_retries: int = 0,
    sleeps: list[float] | None = None,
) -> SiliconFlowSummaryClient:
    return SiliconFlowSummaryClient(
        API_KEY,
        models=models,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=max_retries,
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda: 0,
    )


def test_structured_completion_uses_official_json_mode() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SILICONFLOW_CHAT_URL
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is False
        assert body["model"] == "Qwen/Qwen3-8B"
        return completion(json.dumps(payload(), ensure_ascii=False))

    value, usage = client_for(handle).generate_structured(
        EnrichmentPayload,
        system="system",
        user="输出 JSON",
        max_output_tokens=1000,
    )
    assert value.summary == "摘要"
    assert usage == CompletionUsage(10, 5)


def test_invalid_json_is_repaired_once_and_usage_is_accumulated() -> None:
    responses = iter(
        [
            completion("{bad", input_tokens=10, output_tokens=2),
            completion(
                f"```json\n{json.dumps(payload('修复'), ensure_ascii=False)}\n```",
                input_tokens=8,
                output_tokens=4,
            ),
        ]
    )
    value, usage = client_for(lambda _request: next(responses)).generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "修复"
    assert usage == CompletionUsage(18, 6)


def test_semantic_failure_is_repaired_and_second_failure_is_final() -> None:
    responses = iter(
        [
            completion(json.dumps(payload("first"), ensure_ascii=False)),
            completion(json.dumps(payload("second"), ensure_ascii=False)),
        ]
    )
    client = client_for(
        lambda _request: next(responses),
        models=("Qwen/Qwen3-8B",),
    )
    with pytest.raises(ProviderError) as caught:
        client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
            validator=lambda _value: False,
        )
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED

    invalid_responses = iter([completion("{"), completion("still invalid")])
    with pytest.raises(ProviderError) as invalid:
        client_for(
            lambda _request: next(invalid_responses),
            models=("Qwen/Qwen3-8B",),
        ).generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert invalid.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_input_inspection_retries_once_with_sanitized_transcript() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content)["messages"][1]["content"])
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "content_filter"}})
        return completion(json.dumps(payload("审查后成功"), ensure_ascii=False))

    value, _usage = client_for(handle).generate_structured(
        EnrichmentPayload,
        system="system",
        user="他讨论了赌博和偷渡 请输出 JSON",
        max_output_tokens=1000,
    )
    assert value.summary == "审查后成功"
    assert len(requests) == 2
    assert "赌博" not in requests[1]
    assert "偷渡" not in requests[1]
    assert "相关话题" in requests[1]


def test_retry_after_and_transport_retry() -> None:
    calls = 0
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if calls == 2:
            raise httpx.ReadTimeout("fixture", request=request)
        return completion(json.dumps(payload()))

    value, _ = client_for(handle, max_retries=2, sleeps=sleeps).generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "摘要"
    assert sleeps == [3.0, 2.0]


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (
            httpx.Response(401, json={"error": {"code": "InvalidApiKey"}}),
            ProviderErrorCategory.AUTHENTICATION,
        ),
        (
            httpx.Response(402, json={"code": 30001}),
            ProviderErrorCategory.QUOTA_EXHAUSTED,
        ),
        (
            httpx.Response(
                403,
                json={"error": {"code": "AllocationQuota.FreeTierOnly"}},
            ),
            ProviderErrorCategory.QUOTA_EXHAUSTED,
        ),
        (
            httpx.Response(403, json={"error": {"code": "AccessDenied"}}),
            ProviderErrorCategory.AUTHENTICATION,
        ),
        (httpx.Response(404), ProviderErrorCategory.UNSUPPORTED),
        (httpx.Response(400, text="private body"), ProviderErrorCategory.INVALID_INPUT),
        (httpx.Response(503), ProviderErrorCategory.UNAVAILABLE),
        (
            httpx.Response(200, json={"choices": []}),
            ProviderErrorCategory.SCHEMA_CHANGED,
        ),
    ],
)
def test_api_failures_are_safe(
    response: httpx.Response,
    category: ProviderErrorCategory,
) -> None:
    with pytest.raises(ProviderError) as caught:
        client_for(lambda _request: response).generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert caught.value.failure.category is category
    assert API_KEY not in str(caught.value)
    assert "private body" not in str(caught.value)


def test_transport_exhaustion_and_client_validation() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("fixture", request=request)

    with pytest.raises(ProviderError) as caught:
        client_for(timeout).generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert caught.value.failure.category is ProviderErrorCategory.NETWORK

    with pytest.raises(ValueError, match="cannot be empty"):
        SiliconFlowSummaryClient("")
    with pytest.raises(ValueError, match="negative"):
        SiliconFlowSummaryClient("key", max_retries=-1)


def test_context_manager_with_external_client() -> None:
    transport = httpx.MockTransport(
        lambda _request: completion(json.dumps(payload(), ensure_ascii=False))
    )
    with SiliconFlowSummaryClient(
        "key",
        client=httpx.Client(transport=transport),
    ) as client:
        value, _ = client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert value.summary == "摘要"


def test_schema_failure_is_final_after_the_single_free_model_and_repair_fail() -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requested.append(body["model"])
        return completion("not valid json")

    with pytest.raises(ProviderError) as caught:
        client_for(handle).generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED
    assert caught.value.failure.message == (
        "SiliconFlow JSON repair did not satisfy the summary schema"
    )
    assert requested == [
        "Qwen/Qwen3-8B",
        "Qwen/Qwen3-8B",
    ]


@pytest.mark.parametrize(
    "model",
    [
        "Pro/Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-14B-Instruct",
    ],
)
def test_models_outside_free_allowlist_are_rejected(model: str) -> None:
    with pytest.raises(ValueError, match="free allowlist"):
        SiliconFlowSummaryClient("key", models=(model,))
