"""DashScope qwen-flash JSON summary client."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx
from pydantic import SecretStr

from xyz2notion.enrichment.openai_summary import OpenAICompatibleSummaryClient
from xyz2notion.security import CredentialKind

DASHSCOPE_CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_DASHSCOPE_SUMMARY_MODEL = "qwen-flash"
APPROVED_DASHSCOPE_SUMMARY_MODELS = frozenset({DEFAULT_DASHSCOPE_SUMMARY_MODEL})


class DashScopeSummaryClient(OpenAICompatibleSummaryClient):
    """Fast remote summary client with reasoning explicitly disabled."""

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        model: str = DEFAULT_DASHSCOPE_SUMMARY_MODEL,
        client: httpx.Client | None = None,
        max_retries: int = 1,
        timeout_seconds: float = 120,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            api_key,
            provider="dashscope_summary",
            service_name="DashScope",
            url=DASHSCOPE_CHAT_URL,
            credential_kind=CredentialKind.DASHSCOPE,
            models=(model,),
            allowed_models=APPROVED_DASHSCOPE_SUMMARY_MODELS,
            allowlist_description="approved allowlist",
            completion_limit_field="max_completion_tokens",
            enable_thinking=False,
            client=client,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            sleep=sleep,
            jitter=jitter,
        )
