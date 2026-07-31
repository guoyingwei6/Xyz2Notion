"""Shared structured-summary client contracts and deterministic fallback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

from xyz2notion.enrichment.siliconflow import CompletionUsage
from xyz2notion.models import ProviderError

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class StructuredSummaryClient(Protocol):
    """Minimal interface required by the transcript enrichment pipeline."""

    models: tuple[str, ...]
    active_model: str | None

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
        if self.primary is not None:
            try:
                result = self.primary.generate_structured(
                    model_type,
                    system=system,
                    user=user,
                    max_output_tokens=max_output_tokens,
                    validator=validator,
                )
            except ProviderError:
                pass
            else:
                self.active_model = self.primary.active_model or self.primary.models[0]
                return result
        result = self.fallback.generate_structured(
            model_type,
            system=system,
            user=user,
            max_output_tokens=max_output_tokens,
            validator=validator,
        )
        self.active_model = self.fallback.active_model or self.fallback.models[0]
        return result
