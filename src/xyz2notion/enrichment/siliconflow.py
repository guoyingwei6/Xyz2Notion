"""SiliconFlow free-model JSON summary client."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx
from pydantic import SecretStr

from xyz2notion.enrichment.openai_summary import (
    CompletionUsage,
    OpenAICompatibleSummaryClient,
)
from xyz2notion.security import CredentialKind

__all__ = ["CompletionUsage", "SiliconFlowSummaryClient"]

SILICONFLOW_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_SUMMARY_MODELS = ("Qwen/Qwen3-8B",)
FREE_SUMMARY_MODELS = frozenset(DEFAULT_SUMMARY_MODELS)


class SiliconFlowSummaryClient(OpenAICompatibleSummaryClient):
    """Free-model JSON client with bounded retries and one repair."""

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        models: tuple[str, ...] = DEFAULT_SUMMARY_MODELS,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 180,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            api_key,
            provider="siliconflow_summary",
            service_name="SiliconFlow",
            url=SILICONFLOW_CHAT_URL,
            credential_kind=CredentialKind.SILICONFLOW,
            models=models,
            allowed_models=FREE_SUMMARY_MODELS,
            allowlist_description="free allowlist",
            completion_limit_field="max_tokens",
            enable_thinking=False,
            client=client,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            sleep=sleep,
            jitter=jitter,
        )
