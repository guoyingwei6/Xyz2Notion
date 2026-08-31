"""Lazy, checksum-pinned local Qwen3 structured-summary fallback."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, TypeVar, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from xyz2notion.enrichment.prompts import REPAIR_PROMPT
from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.enrichment.siliconflow import CompletionUsage
from xyz2notion.models import (
    Chapter,
    MindmapNode,
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
)

LOCAL_QWEN_MODEL = "local/Qwen3-1.7B-Q4_K_M"
LOCAL_QWEN_REPOSITORY_REVISION = "7fb011e9aee6e4dc7adf8430df9ea8de6a466aa3"
LOCAL_QWEN_FILENAME = "Qwen3-1.7B-Q4_K_M.gguf"
LOCAL_QWEN_URL = (
    "https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/"
    f"{LOCAL_QWEN_REPOSITORY_REVISION}/{LOCAL_QWEN_FILENAME}"
)
LOCAL_QWEN_SHA256 = "228fb5627f7510b8b3516cdb6435e4b0d2a2bf330fe5b0ab19284a3570a8bb1f"
LOCAL_QWEN_SIZE = 1_107_408_544
LOCAL_QWEN_CONTEXT_TOKENS = 24_576
LOCAL_QWEN_BATCH_TOKENS = 256
LOCAL_QWEN_MAX_OUTPUT_TOKENS = 4_096
LOCAL_QWEN_COMPACT_OUTPUT_TOKENS = 2_048
LOCAL_QWEN_CHUNK_OUTPUT_TOKENS = 1_024
LOCAL_QWEN_TRANSCRIPT_CHUNK_TOKENS = 12_000
StructuredModel = TypeVar("StructuredModel", bound=BaseModel)
GeneratedModel = TypeVar("GeneratedModel", bound=BaseModel)
ModelFactory = Callable[[Path], Any]

CompactItem = Annotated[str, Field(max_length=120)]
CompactName = Annotated[str, Field(max_length=48)]


class LocalChapter(BaseModel):
    """Bounded non-recursive chapter contract for the small local model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=64)
    summary: str = Field(default="", max_length=160)


class LocalEnrichmentPayload(BaseModel):
    """Compact local output converted into the full persisted contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=600)
    chapters: tuple[LocalChapter, ...] = Field(default=(), max_length=8)
    highlights: tuple[CompactItem, ...] = Field(default=(), max_length=6)
    quotes: tuple[CompactItem, ...] = Field(default=(), max_length=5)
    terms: tuple[CompactName, ...] = Field(default=(), max_length=8)
    people: tuple[CompactName, ...] = Field(default=(), max_length=8)
    questions: tuple[CompactItem, ...] = Field(default=(), max_length=6)

    def to_enrichment_payload(self) -> EnrichmentPayload:
        """Build the recursive mind-map deterministically from bounded chapters."""
        chapters = tuple(
            Chapter(
                start_ms=chapter.start_ms,
                title=chapter.title,
                summary=chapter.summary,
            )
            for chapter in self.chapters
        )
        cleaned_summary = self.summary.strip()
        root_title = cleaned_summary.splitlines()[0][:80].strip() if cleaned_summary else "节目摘要"
        mindmap = MindmapNode(
            node_id="root",
            title=root_title,
            children=tuple(
                MindmapNode(
                    node_id=f"chapter-{index}",
                    title=chapter.title,
                )
                for index, chapter in enumerate(chapters, start=1)
            ),
        )
        return EnrichmentPayload(
            summary=self.summary,
            chapters=chapters,
            highlights=self.highlights,
            quotes=self.quotes,
            terms=self.terms,
            people=self.people,
            questions=self.questions,
            mindmap=mindmap,
        )


def _compact_enrichment_prompt(user: str, *, chunk: bool = False) -> str:
    """Replace the recursive contract and explicitly bound local generation."""
    full_schema = json.dumps(
        EnrichmentPayload.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    compact_schema = json.dumps(
        LocalEnrichmentPayload.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    localized = user.replace(full_schema, compact_schema)
    localized = localized.replace(
        "6. mindmap 是以节目主题为根节点的树，每个 node_id 在树内唯一；",
        "6. 不要输出 mindmap；程序会根据 chapters 确定性构建；",
    )
    output_limits = (
        "分段中间结果必须更短：summary 最多 240 字，chapters 最多 4 个，其他数组最多 3 项。"
        if chunk
        else "summary 最多 600 字，chapters 最多 8 个，其他数组严格遵守 Schema 上限。"
    )
    return (
        f"{localized}\n\n"
        "本地紧凑输出限制：只输出 Schema 中列出的字段，不要输出 mindmap；"
        f"{output_limits}"
    )


def _error(
    category: ProviderErrorCategory,
    message: str,
    *,
    code: str | None = None,
) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="local_qwen_summary",
            category=category,
            message=message,
            code=code,
        )
    )


def _default_model_path() -> Path:
    override = os.environ.get("XYZ2NOTION_LOCAL_SUMMARY_MODEL_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "xyz2notion" / "models" / LOCAL_QWEN_FILENAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_llama(path: Path) -> Any:
    try:
        llama_factory = importlib.import_module("llama_cpp").Llama
    except ImportError as exc:
        raise _error(
            ProviderErrorCategory.UNSUPPORTED,
            "Local Qwen runtime is not installed",
        ) from exc
    return llama_factory(
        model_path=str(path),
        n_ctx=LOCAL_QWEN_CONTEXT_TOKENS,
        n_threads=max(1, min(4, os.cpu_count() or 1)),
        n_batch=LOCAL_QWEN_BATCH_TOKENS,
        seed=20260731,
        verbose=False,
    )


def _runtime_failure_code(exc: Exception, *, stage: str) -> str:
    """Classify llama.cpp failures without exposing its raw runtime message."""
    if isinstance(exc, MemoryError):
        return "runtime_memory"
    detail = str(exc).lower()
    if any(marker in detail for marker in ("out of memory", "bad_alloc", "allocate", "mmap")):
        return "runtime_memory"
    if any(marker in detail for marker in ("n_ctx", "context", "kv cache", "token limit")):
        return "runtime_context"
    return f"runtime_{stage}"


class LocalQwenSummaryClient:
    """Download once, verify, and lazily run Qwen3-1.7B on the Actions CPU."""

    models: tuple[str, ...] = (LOCAL_QWEN_MODEL,)
    max_transcript_chunk_tokens = LOCAL_QWEN_TRANSCRIPT_CHUNK_TOKENS

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        client: httpx.Client | None = None,
        model_factory: ModelFactory = _load_llama,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.model_path = Path(model_path) if model_path is not None else _default_model_path()
        self.active_model: str | None = None
        self.active_provider: str | None = "local_qwen_summary"
        self._model_factory = model_factory
        self._progress = progress
        self._model: Any | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=300, follow_redirects=True)

    def _report(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        self._model = None

    def __enter__(self) -> LocalQwenSummaryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ensure_model_file(self) -> Path:
        if (
            self.model_path.is_file()
            and self.model_path.stat().st_size == LOCAL_QWEN_SIZE
            and _sha256(self.model_path) == LOCAL_QWEN_SHA256
        ):
            return self.model_path
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.model_path.with_suffix(f"{self.model_path.suffix}.part")
        digest = hashlib.sha256()
        written = 0
        try:
            with self._client.stream("GET", LOCAL_QWEN_URL) as response:
                if response.is_error:
                    raise _error(
                        ProviderErrorCategory.UNAVAILABLE,
                        f"Local Qwen model download failed (HTTP {response.status_code})",
                        code=str(response.status_code),
                    )
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > LOCAL_QWEN_SIZE:
                            raise _error(
                                ProviderErrorCategory.SCHEMA_CHANGED,
                                "Local Qwen model download exceeded the pinned size",
                            )
                        digest.update(chunk)
                        handle.write(chunk)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _error(
                ProviderErrorCategory.NETWORK,
                f"Local Qwen model download failed: {type(exc).__name__}",
            ) from exc
        if written != LOCAL_QWEN_SIZE or digest.hexdigest() != LOCAL_QWEN_SHA256:
            raise _error(
                ProviderErrorCategory.SCHEMA_CHANGED,
                "Downloaded local Qwen model failed integrity verification",
                code="checksum",
            )
        partial.replace(self.model_path)
        return self.model_path

    def _llama(self) -> Any:
        if self._model is None:
            try:
                self._model = self._model_factory(self._ensure_model_file())
            except ProviderError:
                raise
            except Exception as exc:
                code = _runtime_failure_code(exc, stage="load")
                raise _error(
                    ProviderErrorCategory.UNAVAILABLE,
                    f"Local Qwen model load failed ({code})",
                    code=code,
                ) from exc
        return self._model

    @staticmethod
    def _decode(content: str, model_type: type[StructuredModel]) -> StructuredModel:
        normalized = content.strip()
        if normalized.startswith("```") and normalized.endswith("```"):
            normalized = normalized.removeprefix("```json").removeprefix("```")
            normalized = normalized.removesuffix("```").strip()
        return model_type.model_validate(json.loads(normalized))

    def _complete(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_output_tokens: int,
    ) -> tuple[str, CompletionUsage]:
        try:
            response = self._llama().create_chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{user}\n\n/no_think"},
                ],
                response_format={"type": "json_object", "schema": dict(schema)},
                temperature=0.1,
                max_tokens=min(max_output_tokens, LOCAL_QWEN_MAX_OUTPUT_TOKENS),
            )
        except ProviderError:
            raise
        except Exception as exc:
            code = _runtime_failure_code(exc, stage="inference")
            raise _error(
                ProviderErrorCategory.UNAVAILABLE,
                f"Local Qwen inference failed ({code})",
                code=code,
            ) from exc
        try:
            choice = response["choices"][0]
            message = choice["message"]
            content = message["content"]
            usage = response.get("usage", {})
            if not isinstance(content, str) or not content.strip():
                raise KeyError("content")
            return content.strip(), CompletionUsage(
                input_tokens=max(0, int(usage.get("prompt_tokens", 0))),
                output_tokens=max(0, int(usage.get("completion_tokens", 0))),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise _error(
                ProviderErrorCategory.SCHEMA_CHANGED,
                "Local Qwen returned an unexpected completion schema",
                code="completion_schema",
            ) from exc

    def _generate_model(
        self,
        model_type: type[GeneratedModel],
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        validator: Callable[[GeneratedModel], bool] | None = None,
    ) -> tuple[GeneratedModel, CompletionUsage]:
        """Generate schema-constrained JSON and allow one local repair."""
        schema = model_type.model_json_schema()
        self._report(
            "Local Qwen generation started "
            f"(schema={model_type.__name__}; max_tokens={max_output_tokens})"
        )
        content, usage = self._complete(
            system=system,
            user=user,
            schema=schema,
            max_output_tokens=max_output_tokens,
        )
        self._report(
            "Local Qwen generation finished "
            f"(input_tokens={usage.input_tokens}; output_tokens={usage.output_tokens})"
        )
        try:
            value = self._decode(content, model_type)
            if validator is not None and not validator(value):
                raise ValueError("semantic JSON validation failed")
        except (json.JSONDecodeError, ValidationError, ValueError):
            self._report(
                "Local Qwen JSON repair started "
                f"(schema={model_type.__name__}; max_tokens={max_output_tokens})"
            )
            repaired, repair_usage = self._complete(
                system=system,
                user=REPAIR_PROMPT.format(
                    schema=json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                    invalid=content,
                ),
                schema=schema,
                max_output_tokens=max_output_tokens,
            )
            usage += repair_usage
            self._report(
                "Local Qwen JSON repair finished "
                f"(input_tokens={repair_usage.input_tokens}; "
                f"output_tokens={repair_usage.output_tokens})"
            )
            try:
                value = self._decode(repaired, model_type)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise _error(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Local Qwen JSON repair did not satisfy the summary schema",
                    code="summary_schema",
                ) from exc
            if validator is not None and not validator(value):
                raise _error(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Local Qwen JSON repair did not satisfy timeline constraints",
                    code="timeline_constraints",
                ) from None
        self.active_model = LOCAL_QWEN_MODEL
        return value, usage

    def generate_structured(
        self,
        model_type: type[StructuredModel],
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        validator: Callable[[StructuredModel], bool] | None = None,
    ) -> tuple[StructuredModel, CompletionUsage]:
        """Use a compact non-recursive contract for full local enrichment."""
        if model_type is EnrichmentPayload:
            chunk = user.lstrip().startswith("这是长播客的第 ")
            compact_validator = (
                None
                if validator is None
                else lambda value: validator(cast(StructuredModel, value.to_enrichment_payload()))
            )
            compact, usage = self._generate_model(
                LocalEnrichmentPayload,
                system=system,
                user=_compact_enrichment_prompt(user, chunk=chunk),
                max_output_tokens=min(
                    max_output_tokens,
                    (LOCAL_QWEN_CHUNK_OUTPUT_TOKENS if chunk else LOCAL_QWEN_COMPACT_OUTPUT_TOKENS),
                ),
                validator=compact_validator,
            )
            return cast(StructuredModel, compact.to_enrichment_payload()), usage
        return self._generate_model(
            model_type,
            system=system,
            user=user,
            max_output_tokens=max_output_tokens,
            validator=validator,
        )
