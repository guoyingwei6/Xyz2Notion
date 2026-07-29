"""Official DashScope OpenAI-compatible JSON summary client."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from xyz2notion.enrichment.prompts import REPAIR_PROMPT
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
)
from xyz2notion.security import CredentialKind, validate_credential_destination

DASHSCOPE_CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


@dataclass(frozen=True)
class CompletionUsage:
    """Token accounting returned by the official compatible endpoint."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: CompletionUsage) -> CompletionUsage:
        return CompletionUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class StructuredCompletion:
    """Validated object plus all tokens spent, including JSON repair."""

    value: BaseModel
    usage: CompletionUsage


def _error(
    category: ProviderErrorCategory,
    message: str,
    *,
    code: str | None = None,
) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="dashscope",
            category=category,
            message=message,
            code=code,
        )
    )


def _response_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code") or error.get("type")
    else:
        code = payload.get("code")
    return str(code) if code is not None else None


class DashScopeSummaryClient:
    """Low-level Qwen JSON client with bounded retries and one repair call."""

    _retryable_statuses = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 180,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret.strip():
            raise ValueError("DashScope API key cannot be empty")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        validate_credential_destination(DASHSCOPE_CHAT_URL, CredentialKind.DASHSCOPE)
        self.max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DashScopeSummaryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return max(0.0, float(response.headers["Retry-After"]))
            except ValueError:
                pass
        return min(30.0, 2.0**attempt) + self._jitter()

    def _complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_output_tokens: int,
    ) -> tuple[str, CompletionUsage]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "temperature": 0.1,
            "max_tokens": max_output_tokens,
        }
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.post(
                    DASHSCOPE_CHAT_URL,
                    headers=self._headers,
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    self._sleep(self._delay(None, attempt))
                    continue
                raise _error(
                    ProviderErrorCategory.NETWORK,
                    f"DashScope transport failed: {type(exc).__name__}",
                ) from exc
            code = _response_code(response)
            if response.status_code in self._retryable_statuses:
                if attempt < self.max_retries:
                    self._sleep(self._delay(response, attempt))
                    continue
                category = (
                    ProviderErrorCategory.RATE_LIMITED
                    if response.status_code == 429
                    else ProviderErrorCategory.UNAVAILABLE
                )
                raise _error(
                    category,
                    f"DashScope is temporarily unavailable (HTTP {response.status_code})",
                    code=code or str(response.status_code),
                )
            if response.status_code == 401:
                raise _error(
                    ProviderErrorCategory.AUTHENTICATION,
                    "DashScope API key is invalid",
                    code=code or "401",
                )
            if response.status_code == 403:
                category = (
                    ProviderErrorCategory.QUOTA_EXHAUSTED
                    if code == "AllocationQuota.FreeTierOnly"
                    else ProviderErrorCategory.AUTHENTICATION
                )
                message = (
                    "DashScope free quota is exhausted and Free Quota Only stopped the call"
                    if category is ProviderErrorCategory.QUOTA_EXHAUSTED
                    else "DashScope denied access"
                )
                raise _error(category, message, code=code or "403")
            if response.status_code == 404:
                raise _error(
                    ProviderErrorCategory.UNSUPPORTED,
                    "Configured DashScope model or endpoint is unavailable",
                    code=code or "404",
                )
            if response.is_error:
                raise _error(
                    ProviderErrorCategory.INVALID_INPUT,
                    f"DashScope rejected the summary request (HTTP {response.status_code})",
                    code=code or str(response.status_code),
                )
            try:
                decoded = response.json()
                choice = decoded["choices"][0]
                content = choice["message"]["content"]
                usage = decoded.get("usage", {})
                if not isinstance(content, str) or not content.strip():
                    raise KeyError("content")
                return content.strip(), CompletionUsage(
                    input_tokens=max(0, int(usage.get("prompt_tokens", 0))),
                    output_tokens=max(0, int(usage.get("completion_tokens", 0))),
                )
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise _error(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "DashScope returned an unexpected completion schema",
                ) from exc
        raise AssertionError("unreachable retry loop")

    @staticmethod
    def _decode(content: str, model_type: type[StructuredModel]) -> StructuredModel:
        normalized = content.strip()
        if normalized.startswith("```") and normalized.endswith("```"):
            normalized = normalized.removeprefix("```json").removeprefix("```")
            normalized = normalized.removesuffix("```").strip()
        decoded = json.loads(normalized)
        return model_type.model_validate(decoded)

    def generate_structured(
        self,
        model_type: type[StructuredModel],
        *,
        model: str,
        system: str,
        user: str,
        max_output_tokens: int,
        validator: Callable[[StructuredModel], bool] | None = None,
    ) -> tuple[StructuredModel, CompletionUsage]:
        """Generate one object and repair only its JSON when validation fails."""
        content, usage = self._complete(
            model=model,
            system=system,
            user=user,
            max_output_tokens=max_output_tokens,
        )
        try:
            value = self._decode(content, model_type)
            if validator is not None and not validator(value):
                raise ValueError("semantic JSON validation failed")
            return value, usage
        except (json.JSONDecodeError, ValidationError, ValueError):
            schema = json.dumps(
                model_type.model_json_schema(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            repaired, repair_usage = self._complete(
                model=model,
                system=system,
                user=REPAIR_PROMPT.format(schema=schema, invalid=content),
                max_output_tokens=max_output_tokens,
            )
            total_usage = usage + repair_usage
            try:
                value = self._decode(repaired, model_type)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise _error(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "DashScope JSON repair did not satisfy the summary schema",
                ) from exc
            if validator is not None and not validator(value):
                raise _error(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "DashScope JSON repair did not satisfy timeline constraints",
                ) from None
            return value, total_usage
