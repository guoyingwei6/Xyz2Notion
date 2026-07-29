"""Provider fallback policy independent of ASR orchestration state."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from xyz2notion.models import ProviderError, ProviderErrorCategory

T = TypeVar("T")

_TINGWU_FALLBACK_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.AUTHENTICATION,
        ProviderErrorCategory.QUOTA_EXHAUSTED,
        ProviderErrorCategory.RISK_CONTROL,
        ProviderErrorCategory.SCHEMA_CHANGED,
        ProviderErrorCategory.UNSUPPORTED,
        ProviderErrorCategory.UNKNOWN,
    }
)


def tingwu_fallback_allowed(error: ProviderError) -> bool:
    """Return whether a final Tingwu failure may switch to SiliconFlow."""
    return error.failure.category in _TINGWU_FALLBACK_CATEGORIES


def run_with_tingwu_fallback(
    tingwu: Callable[[], T],
    siliconflow: Callable[[], T],
) -> T:
    """Run the fallback only for an explicit final Tingwu failure.

    A submitted/processing task is a normal return value and therefore never
    invokes the fallback.
    """
    try:
        return tingwu()
    except ProviderError as exc:
        if not tingwu_fallback_allowed(exc):
            raise
        return siliconflow()
