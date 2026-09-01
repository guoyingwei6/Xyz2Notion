import json
from collections.abc import Callable

import httpx
import pytest

from xyz2notion.enrichment.dashscope import (
    DASHSCOPE_CHAT_URL,
    DashScopeSummaryClient,
)
from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.enrichment.siliconflow import CompletionUsage
from xyz2notion.models import ProviderError, ProviderErrorCategory

API_KEY = "dashscope-fixture-secret"


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
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    )


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 0,
    sleeps: list[float] | None = None,
) -> DashScopeSummaryClient:
    return DashScopeSummaryClient(
        API_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=max_retries,
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda: 0,
    )


def test_structured_completion_uses_qwen_flash_without_thinking() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == DASHSCOPE_CHAT_URL
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        assert body["model"] == "qwen-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is False
        assert body["max_completion_tokens"] == 1000
        assert "max_tokens" not in body
        return completion(json.dumps(payload(), ensure_ascii=False))

    client = client_for(handle)
    value, usage = client.generate_structured(
        EnrichmentPayload,
        system="system",
        user="输出 JSON",
        max_output_tokens=1000,
    )
    assert value.summary == "摘要"
    assert usage == CompletionUsage(10, 5)
    assert client.active_model == "qwen-flash"
    assert client.active_provider == "dashscope_summary"


def test_invalid_json_is_repaired_once() -> None:
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


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (
            httpx.Response(401, json={"error": {"code": "InvalidApiKey"}}),
            ProviderErrorCategory.AUTHENTICATION,
        ),
        (
            httpx.Response(402, json={"error": {"code": "Arrearage"}}),
            ProviderErrorCategory.QUOTA_EXHAUSTED,
        ),
        (
            httpx.Response(403, json={"error": {"code": "AllocationQuota.Exhausted"}}),
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
def test_api_failures_are_classified_without_leaking_responses(
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
    assert caught.value.failure.provider == "dashscope_summary"
    assert caught.value.failure.category is category
    assert API_KEY not in str(caught.value)
    assert "private body" not in str(caught.value)


def test_retry_and_client_validation_are_bounded() -> None:
    calls = 0
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("fixture", request=request)
        return completion(json.dumps(payload()))

    value, _usage = client_for(handle, max_retries=1, sleeps=sleeps).generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "摘要"
    assert sleeps == [1.0]

    with pytest.raises(ValueError, match="cannot be empty"):
        DashScopeSummaryClient("")
    with pytest.raises(ValueError, match="approved allowlist"):
        DashScopeSummaryClient("key", model="qwen-plus")
    with pytest.raises(ValueError, match="negative"):
        DashScopeSummaryClient("key", max_retries=-1)


def test_context_manager_with_external_client() -> None:
    transport = httpx.MockTransport(
        lambda _request: completion(json.dumps(payload(), ensure_ascii=False))
    )
    with DashScopeSummaryClient(
        "key",
        client=httpx.Client(transport=transport),
    ) as client:
        value, _usage = client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert value.summary == "摘要"
