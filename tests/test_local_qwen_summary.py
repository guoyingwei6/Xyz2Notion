import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from xyz2notion.enrichment.client import FallbackSummaryClient, preflight_summary_client
from xyz2notion.enrichment.local_qwen import (
    LOCAL_QWEN_BATCH_TOKENS,
    LOCAL_QWEN_CONTEXT_TOKENS,
    LOCAL_QWEN_MAX_OUTPUT_TOKENS,
    LOCAL_QWEN_MODEL,
    LocalQwenSummaryClient,
    _default_model_path,
    _load_llama,
)
from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.enrichment.siliconflow import CompletionUsage
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
)


def payload(summary: str = "本地摘要") -> dict[str, object]:
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


class FakeLlama:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.requests: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs: object) -> dict[str, object]:
        self.requests.append(dict(kwargs))
        return {
            "choices": [{"message": {"content": next(self.responses)}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }


def local_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[str],
) -> tuple[LocalQwenSummaryClient, FakeLlama]:
    content = b"pinned-model"
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(content)
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SIZE", len(content))
    monkeypatch.setattr(
        "xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SHA256",
        hashlib.sha256(content).hexdigest(),
    )
    model = FakeLlama(responses)
    return (
        LocalQwenSummaryClient(
            model_path=model_path,
            model_factory=lambda _path: model,
        ),
        model,
    )


def test_local_qwen_generates_schema_constrained_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = local_client(
        tmp_path,
        monkeypatch,
        [json.dumps(payload(), ensure_ascii=False)],
    )
    value, usage = client.generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=10_000,
    )
    assert value.summary == "本地摘要"
    assert usage == CompletionUsage(20, 10)
    assert client.active_model == LOCAL_QWEN_MODEL
    assert client.active_provider == "local_qwen_summary"
    assert model.requests[0]["max_tokens"] == LOCAL_QWEN_MAX_OUTPUT_TOKENS
    assert model.requests[0]["response_format"] == {
        "type": "json_object",
        "schema": EnrichmentPayload.model_json_schema(),
    }
    messages = model.requests[0]["messages"]
    assert isinstance(messages, list)
    assert "/no_think" in str(messages[-1])


def test_local_qwen_repairs_invalid_json_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = local_client(
        tmp_path,
        monkeypatch,
        ["not-json", json.dumps(payload("修复成功"), ensure_ascii=False)],
    )
    value, usage = client.generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "修复成功"
    assert usage == CompletionUsage(40, 20)
    assert len(model.requests) == 2


def test_local_qwen_download_is_checksum_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"downloaded-model"
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SIZE", len(content))
    monkeypatch.setattr(
        "xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SHA256",
        hashlib.sha256(content).hexdigest(),
    )
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=content))
    model_path = tmp_path / "download.gguf"
    client = LocalQwenSummaryClient(
        model_path=model_path,
        client=httpx.Client(transport=transport),
        model_factory=lambda _path: FakeLlama([json.dumps(payload())]),
    )
    assert client._ensure_model_file() == model_path
    assert model_path.read_bytes() == content


def test_local_qwen_replaces_corrupt_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "corrupt.gguf"
    model_path.write_bytes(b"corrupt")
    replacement = b"valid-model"
    monkeypatch.setattr(
        "xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SIZE",
        len(replacement),
    )
    monkeypatch.setattr(
        "xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SHA256",
        hashlib.sha256(replacement).hexdigest(),
    )
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=replacement))
    client = LocalQwenSummaryClient(
        model_path=model_path,
        client=httpx.Client(transport=transport),
    )
    assert client._ensure_model_file() == model_path
    assert model_path.read_bytes() == replacement


def test_default_model_path_honors_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "configured.gguf"
    monkeypatch.setenv("XYZ2NOTION_LOCAL_SUMMARY_MODEL_PATH", str(configured))
    assert _default_model_path() == configured
    monkeypatch.delenv("XYZ2NOTION_LOCAL_SUMMARY_MODEL_PATH")
    assert _default_model_path().name == "Qwen3-1.7B-Q4_K_M.gguf"


def test_load_llama_reports_missing_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> object:
        raise ImportError

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ProviderError) as caught:
        _load_llama(Path("model.gguf"))
    assert caught.value.failure.category is ProviderErrorCategory.UNSUPPORTED


def test_load_llama_uses_bounded_cpu_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: SimpleNamespace(Llama=factory),
    )
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.os.cpu_count", lambda: 32)
    _load_llama(Path("model.gguf"))
    assert captured["n_threads"] == 4
    assert captured["n_ctx"] == LOCAL_QWEN_CONTEXT_TOKENS
    assert captured["n_batch"] == LOCAL_QWEN_BATCH_TOKENS
    assert captured["verbose"] is False


@pytest.mark.parametrize(
    ("handler", "category"),
    [
        (
            lambda _request: httpx.Response(503),
            ProviderErrorCategory.UNAVAILABLE,
        ),
        (
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)),
            ProviderErrorCategory.NETWORK,
        ),
    ],
)
def test_local_qwen_download_reports_safe_provider_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
    category: ProviderErrorCategory,
) -> None:
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SIZE", 10)
    client = LocalQwenSummaryClient(
        model_path=tmp_path / "model.gguf",
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )
    with pytest.raises(ProviderError) as caught:
        client._ensure_model_file()
    assert caught.value.failure.category is category


@pytest.mark.parametrize(
    ("content", "size"),
    [
        (b"too-large", 2),
        (b"wrong", 5),
    ],
)
def test_local_qwen_download_rejects_unpinned_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    size: int,
) -> None:
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SIZE", size)
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SHA256", "0" * 64)
    client = LocalQwenSummaryClient(
        model_path=tmp_path / "model.gguf",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=content))
        ),
    )
    with pytest.raises(ProviderError) as caught:
        client._ensure_model_file()
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_local_qwen_context_closes_owned_client_and_drops_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _model = local_client(
        tmp_path,
        monkeypatch,
        [json.dumps(payload(), ensure_ascii=False)],
    )
    with client as entered:
        assert entered is client
        assert client._llama() is client._llama()
    assert client._model is None
    assert client._client.is_closed


def test_local_qwen_accepts_fenced_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fenced = f"```json\n{json.dumps(payload(), ensure_ascii=False)}\n```"
    client, _model = local_client(tmp_path, monkeypatch, [fenced])
    value, _usage = client.generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "本地摘要"


@pytest.mark.parametrize("response", [{}, {"choices": []}])
def test_local_qwen_rejects_unexpected_completion_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    client, _model = local_client(tmp_path, monkeypatch, [])
    client._model = SimpleNamespace(create_chat_completion=lambda **_kwargs: response)
    with pytest.raises(ProviderError) as caught:
        client._complete(system="system", user="user", schema={}, max_output_tokens=100)
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_local_qwen_maps_runtime_failure_to_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _model = local_client(tmp_path, monkeypatch, [])

    def fail(**_kwargs: object) -> object:
        raise RuntimeError("private runtime detail")

    client._model = SimpleNamespace(create_chat_completion=fail)
    with pytest.raises(ProviderError) as caught:
        client._complete(system="system", user="user", schema={}, max_output_tokens=100)
    assert caught.value.failure.category is ProviderErrorCategory.UNAVAILABLE
    assert caught.value.failure.code == "runtime_inference"
    assert "private runtime detail" not in caught.value.failure.message


def test_local_qwen_classifies_safe_load_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"pinned-model"
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(content)
    monkeypatch.setattr("xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SIZE", len(content))
    monkeypatch.setattr(
        "xyz2notion.enrichment.local_qwen.LOCAL_QWEN_SHA256",
        hashlib.sha256(content).hexdigest(),
    )

    def fail_load(_path: Path) -> object:
        raise RuntimeError("failed to allocate private model path")

    client = LocalQwenSummaryClient(model_path=model_path, model_factory=fail_load)
    with pytest.raises(ProviderError) as caught:
        client._llama()
    assert caught.value.failure.code == "runtime_memory"
    assert "private model path" not in caught.value.failure.message


def test_local_qwen_repair_failure_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = local_client(tmp_path, monkeypatch, ["invalid", "still invalid"])
    with pytest.raises(ProviderError) as caught:
        client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED
    assert len(model.requests) == 2


def test_local_qwen_repair_must_pass_semantic_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = json.dumps(payload(), ensure_ascii=False)
    client, model = local_client(tmp_path, monkeypatch, [encoded, encoded])
    with pytest.raises(ProviderError) as caught:
        client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
            validator=lambda _value: False,
        )
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED
    assert len(model.requests) == 2


def test_fallback_client_uses_local_after_any_remote_provider_failure() -> None:
    class Remote:
        models = ("Qwen/Qwen3-8B",)
        active_model = None

        def generate_structured(self, *_args: object, **_kwargs: object) -> object:
            raise ProviderError(
                ProviderFailure(
                    provider="siliconflow_summary",
                    category=ProviderErrorCategory.INVALID_INPUT,
                    message="safe fixture",
                )
            )

    class Local:
        models = (LOCAL_QWEN_MODEL,)
        active_model = LOCAL_QWEN_MODEL

        def generate_structured(self, *_args: object, **_kwargs: object) -> object:
            return EnrichmentPayload.model_validate(payload()), CompletionUsage()

    client = FallbackSummaryClient(Remote(), Local())  # type: ignore[arg-type]
    value, _ = client.generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "本地摘要"
    assert client.active_model == LOCAL_QWEN_MODEL
    assert client.active_provider == "local_qwen_summary"


def test_fallback_client_keeps_remote_success() -> None:
    remote = SimpleNamespace(
        models=("Qwen/Qwen3-8B",),
        active_model="Qwen/Qwen3-8B",
        generate_structured=lambda *_args, **_kwargs: (
            EnrichmentPayload.model_validate(payload("远程成功")),
            CompletionUsage(1, 1),
        ),
    )
    fallback = SimpleNamespace(
        models=(LOCAL_QWEN_MODEL,),
        active_model=None,
        generate_structured=lambda *_args, **_kwargs: pytest.fail("fallback should not run"),
    )
    client = FallbackSummaryClient(remote, fallback)  # type: ignore[arg-type]
    value, _ = client.generate_structured(
        EnrichmentPayload,
        system="system",
        user="user",
        max_output_tokens=1000,
    )
    assert value.summary == "远程成功"
    assert client.active_model == "Qwen/Qwen3-8B"
    assert client.active_provider == "siliconflow_summary"


def test_fallback_client_preserves_both_safe_failure_layers() -> None:
    def fail(provider: str, category: ProviderErrorCategory, code: str) -> object:
        def generate(*_args: object, **_kwargs: object) -> object:
            raise ProviderError(
                ProviderFailure(
                    provider=provider,
                    category=category,
                    message="safe fixture",
                    code=code,
                )
            )

        return SimpleNamespace(
            models=(provider,),
            active_model=None,
            generate_structured=generate,
        )

    client = FallbackSummaryClient(
        fail("siliconflow_summary", ProviderErrorCategory.QUOTA_EXHAUSTED, "quota.code"),
        fail("local_qwen_summary", ProviderErrorCategory.UNAVAILABLE, "runtime_load"),
    )
    with pytest.raises(ProviderError) as caught:
        client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    failure = caught.value.failure
    assert failure.provider == "summary_fallback_chain"
    assert failure.category is ProviderErrorCategory.UNAVAILABLE
    assert failure.message == (
        "Summary providers failed: "
        "primary=siliconflow_summary:quota_exhausted:quota.code; "
        "fallback=local_qwen_summary:unavailable:runtime_load"
    )


def test_summary_preflight_reports_primary_failure_and_working_fallback() -> None:
    class Remote:
        models = ("Qwen/Qwen3-8B",)
        active_model = None

        def generate_structured(self, *_args: object, **_kwargs: object) -> object:
            raise ProviderError(
                ProviderFailure(
                    provider="siliconflow_summary",
                    category=ProviderErrorCategory.QUOTA_EXHAUSTED,
                    message="safe fixture",
                    code="quota.code",
                )
            )

    class Local:
        models = (LOCAL_QWEN_MODEL,)
        active_model = LOCAL_QWEN_MODEL
        active_provider = "local_qwen_summary"

        def generate_structured(
            self,
            model_type: object,
            *_args: object,
            **_kwargs: object,
        ) -> object:
            return model_type(ok=True), CompletionUsage()  # type: ignore[operator]

    result = preflight_summary_client(  # type: ignore[arg-type]
        FallbackSummaryClient(Remote(), Local())  # type: ignore[arg-type]
    )
    assert result.provider == "local_qwen_summary"
    assert result.model == LOCAL_QWEN_MODEL
    assert result.summary() == (
        "Summary route preflight OK "
        f"(active_provider=local_qwen_summary; active_model={LOCAL_QWEN_MODEL}; "
        "primary=siliconflow_summary:quota_exhausted:quota.code; fallback=ok)"
    )


def test_fallback_client_without_remote_and_context_cleanup() -> None:
    closed: list[bool] = []
    fallback = SimpleNamespace(
        models=(LOCAL_QWEN_MODEL,),
        active_model=None,
        close=lambda: closed.append(True),
        generate_structured=lambda *_args, **_kwargs: (
            EnrichmentPayload.model_validate(payload()),
            CompletionUsage(),
        ),
    )
    with FallbackSummaryClient(None, fallback) as client:  # type: ignore[arg-type]
        value, _usage = client.generate_structured(
            EnrichmentPayload,
            system="system",
            user="user",
            max_output_tokens=1000,
        )
    assert value.summary == "本地摘要"
    assert client.active_model == LOCAL_QWEN_MODEL
    assert client.active_provider == "local_qwen_summary"
    assert closed == [True]
