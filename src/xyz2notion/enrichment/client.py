"""Shared structured-summary client contracts and deterministic fallback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from xyz2notion.enrichment.siliconflow import CompletionUsage
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
)

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)
SUMMARY_FALLBACK_PROVIDER = "summary_fallback_chain"


class _SummaryPreflightPayload(BaseModel):
    """Tiny fixed-schema response used before real transcript content."""

    model_config = ConfigDict(extra="forbid")

    ok: bool


@dataclass(frozen=True)
class SummaryPreflightResult:
    """Secret-free health result for the active summary route."""

    provider: str
    model: str
    primary_failure: ProviderFailure | None = None

    def summary(self) -> str:
        primary = (
            _failure_token(self.primary_failure)
            if self.primary_failure is not None
            else ("not_configured" if self.provider == "local_qwen_summary" else "ok")
        )
        fallback = "ok" if self.provider == "local_qwen_summary" else "not_tested"
        return (
            "Summary route preflight OK "
            f"(active_provider={self.provider}; active_model={self.model}; "
            f"primary={primary}; fallback={fallback})"
        )


def _safe_code(code: str | None) -> str:
    if code is None:
        return ""
    return "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in code
    )[:80]


def _failure_token(failure: ProviderFailure) -> str:
    code = _safe_code(failure.code)
    suffix = f":{code}" if code else ""
    return f"{failure.provider}:{failure.category.value}{suffix}"


def _combined_failure(primary: ProviderFailure, fallback: ProviderFailure) -> ProviderError:
    """Preserve both safe failure layers while retaining useful retry semantics."""
    if fallback.retryable:
        category = fallback.category
    elif primary.retryable:
        category = primary.category
    else:
        category = fallback.category
    return ProviderError(
        ProviderFailure(
            provider=SUMMARY_FALLBACK_PROVIDER,
            category=category,
            message=(
                "Summary providers failed: "
                f"primary={_failure_token(primary)}; "
                f"fallback={_failure_token(fallback)}"
            ),
            code=_safe_code(fallback.code) or None,
        )
    )


class StructuredSummaryClient(Protocol):
    """Minimal interface required by the transcript enrichment pipeline."""

    models: tuple[str, ...]
    active_model: str | None
    active_provider: str | None

    def close(self) -> None: ...

    def __enter__(self) -> StructuredSummaryClient: ...

    def __exit__(self, *_args: object) -> None: ...

    def generate_structured(
        self,
        model_type: type[StructuredModel],
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        validator: Callable[[StructuredModel], bool] | None = None,
    ) -> tuple[StructuredModel, CompletionUsage]: ...


class FallbackSummaryClient:
    """Try one remote free model, then one local model for any provider failure."""

    def __init__(
        self,
        primary: StructuredSummaryClient | None,
        fallback: StructuredSummaryClient,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.models = (
            *(primary.models if primary is not None else ()),
            *fallback.models,
        )
        self.active_model: str | None = None
        self.active_provider: str | None = None
        self.last_primary_failure: ProviderFailure | None = None
        chunk_limits = tuple(
            limit
            for candidate in (primary, fallback)
            if isinstance(
                (limit := getattr(candidate, "max_transcript_chunk_tokens", None)),
                int,
            )
            and limit > 0
        )
        self.max_transcript_chunk_tokens: int | None = min(chunk_limits) if chunk_limits else None

    def close(self) -> None:
        for client in (self.primary, self.fallback):
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> FallbackSummaryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def generate_structured(
        self,
        model_type: type[StructuredModel],
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        validator: Callable[[StructuredModel], bool] | None = None,
    ) -> tuple[StructuredModel, CompletionUsage]:
        primary_failure: ProviderFailure | None = None
        self.last_primary_failure = None
        if self.primary is not None:
            try:
                result = self.primary.generate_structured(
                    model_type,
                    system=system,
                    user=user,
                    max_output_tokens=max_output_tokens,
                    validator=validator,
                )
            except ProviderError as exc:
                primary_failure = exc.failure
                self.last_primary_failure = exc.failure
            else:
                self.active_model = self.primary.active_model or self.primary.models[0]
                self.active_provider = (
                    getattr(self.primary, "active_provider", None) or "siliconflow_summary"
                )
                return result
        try:
            result = self.fallback.generate_structured(
                model_type,
                system=system,
                user=user,
                max_output_tokens=max_output_tokens,
                validator=validator,
            )
        except ProviderError as exc:
            if primary_failure is None:
                raise
            raise _combined_failure(primary_failure, exc.failure) from exc
        self.active_model = self.fallback.active_model or self.fallback.models[0]
        self.active_provider = (
            getattr(self.fallback, "active_provider", None) or "local_qwen_summary"
        )
        return result


def preflight_summary_client(client: StructuredSummaryClient) -> SummaryPreflightResult:
    """Exercise the configured route with fixed, non-user JSON before real work."""
    value, _usage = client.generate_structured(
        _SummaryPreflightPayload,
        system="Return only a JSON object that satisfies the supplied schema.",
        user='Return exactly {"ok":true}.',
        max_output_tokens=512,
        validator=lambda payload: payload.ok,
    )
    if not value.ok:
        raise ProviderError(
            ProviderFailure(
                provider=getattr(client, "active_provider", None) or "summary_preflight",
                category=ProviderErrorCategory.SCHEMA_CHANGED,
                message="Summary route preflight returned an invalid result",
                code="preflight_invalid",
            )
        )
    provider = getattr(client, "active_provider", None) or "summary_unknown"
    model = getattr(client, "active_model", None) or client.models[0]
    primary_failure = getattr(client, "last_primary_failure", None)
    return SummaryPreflightResult(
        provider=provider,
        model=model,
        primary_failure=(primary_failure if isinstance(primary_failure, ProviderFailure) else None),
    )
