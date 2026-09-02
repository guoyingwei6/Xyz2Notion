"""Reusable JSON-summary client for approved OpenAI-compatible endpoints."""

from __future__ import annotations

import json
import random
import re
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

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)

_DATA_INSPECTION_CODES = frozenset(
    {
        "data_inspection_failed",
        "datainspectionfailed",
        "content_filter",
        "content_filter_error",
        "sensitive_content_detected",
        "safety_risk",
    }
)

_RISK_TERM = re.compile(
    r"(?:杀生|杀人|自杀|自尽|吸毒|贩毒|毒品|海洛因|可卡因|大麻|鸦片|芬太尼|"
    r"枪支|开枪|枪击|弹药|炸药|爆炸|炸弹|假证|偷渡|走私|绑架|贩运|贩子|黑帮|"
    r"尸体|死人|死尸|亡者|凶杀|凶手|血腥|色情|裸体|嫖娼|赌博|赌场|缅甸)"
)


@dataclass(frozen=True)
class CompletionUsage:
    """Provider-neutral token accounting for one or more completions."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: CompletionUsage) -> CompletionUsage:
        return CompletionUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


def _safe_code(value: object | None) -> str | None:
    if value is None:
        return None
    code = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in str(value)
    )[:80]
    return code or None


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
    return _safe_code(code)


def _looks_like_quota_error(code: str | None) -> bool:
    if code is None:
        return False
    normalized = code.lower()
    return any(
        marker in normalized for marker in ("quota", "balance", "arrear", "insufficient", "30001")
    )


class OpenAICompatibleSummaryClient:
    """Bounded JSON generation, validation, and one repair for one provider."""

    _retryable_statuses = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        provider: str,
        service_name: str,
        url: str,
        credential_kind: CredentialKind,
        models: tuple[str, ...],
        allowed_models: frozenset[str],
        allowlist_description: str,
        completion_limit_field: str,
        enable_thinking: bool | None,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 180,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret.strip():
            raise ValueError(f"{service_name} API key cannot be empty")
        normalized_models = tuple(model.strip() for model in models if model.strip())
        if not normalized_models:
            raise ValueError(f"at least one {service_name} summary model is required")
        if unknown := set(normalized_models) - allowed_models:
            raise ValueError(
                f"{service_name} summary models outside the {allowlist_description} "
                "are not allowed: "
                f"{', '.join(sorted(unknown))}"
            )
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if completion_limit_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported completion token parameter")
        validate_credential_destination(url, credential_kind)
        self.models = normalized_models
        self.active_model: str | None = None
        self.active_provider: str | None = provider
        self.max_retries = max_retries
        self._provider = provider
        self._service_name = service_name
        self._url = url
        self._completion_limit_field = completion_limit_field
        self._enable_thinking = enable_thinking
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

    def __enter__(self) -> OpenAICompatibleSummaryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _error(
        self,
        category: ProviderErrorCategory,
        message: str,
        *,
        code: str | None = None,
    ) -> ProviderError:
        return ProviderError(
            ProviderFailure(
                provider=self._provider,
                category=category,
                message=message,
                code=_safe_code(code),
            )
        )

    @staticmethod
    def _code_marks_input_inspection(code: str | None) -> bool:
        if not code:
            return False
        normalized = code.lower()
        return any(marker in normalized for marker in _DATA_INSPECTION_CODES)

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
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            self._completion_limit_field: max_output_tokens,
        }
        if self._enable_thinking is not None:
            payload["enable_thinking"] = self._enable_thinking
        normal_attempts = 0
        inspection_retry_attempted = False
        while True:
            response: httpx.Response | None = None
            try:
                response = self._client.post(
                    self._url,
                    headers=self._headers,
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if normal_attempts < self.max_retries:
                    self._sleep(self._delay(None, normal_attempts))
                    normal_attempts += 1
                    continue
                raise self._error(
                    ProviderErrorCategory.NETWORK,
                    f"{self._service_name} summary transport failed: {type(exc).__name__}",
                ) from exc
            code = _response_code(response)
            if response.status_code in self._retryable_statuses:
                if normal_attempts < self.max_retries:
                    self._sleep(self._delay(response, normal_attempts))
                    normal_attempts += 1
                    continue
                category = (
                    ProviderErrorCategory.RATE_LIMITED
                    if response.status_code == 429
                    else ProviderErrorCategory.UNAVAILABLE
                )
                raise self._error(
                    category,
                    f"{self._service_name} summary is temporarily unavailable "
                    f"(HTTP {response.status_code})",
                    code=code or str(response.status_code),
                )
            if response.status_code == 401:
                raise self._error(
                    ProviderErrorCategory.AUTHENTICATION,
                    f"{self._service_name} API key is invalid",
                    code=code or "401",
                )
            if response.status_code == 402:
                raise self._error(
                    ProviderErrorCategory.QUOTA_EXHAUSTED,
                    f"{self._service_name} summary quota or account balance is unavailable",
                    code=code or "402",
                )
            if response.status_code == 403:
                category = (
                    ProviderErrorCategory.QUOTA_EXHAUSTED
                    if _looks_like_quota_error(code)
                    else ProviderErrorCategory.AUTHENTICATION
                )
                raise self._error(
                    category,
                    (
                        f"{self._service_name} summary quota is unavailable"
                        if category is ProviderErrorCategory.QUOTA_EXHAUSTED
                        else f"{self._service_name} denied access"
                    ),
                    code=code or "403",
                )
            if response.status_code == 404:
                raise self._error(
                    ProviderErrorCategory.UNSUPPORTED,
                    f"Configured {self._service_name} summary model is unavailable",
                    code=code or "404",
                )
            if response.is_error:
                if (
                    response.status_code == 400
                    and self._code_marks_input_inspection(code)
                    and not inspection_retry_attempted
                    and _RISK_TERM.search(user)
                ):
                    sanitized_user = _RISK_TERM.sub("相关话题", user)
                    if sanitized_user != user:
                        inspection_retry_attempted = True
                        user = sanitized_user
                        messages[1]["content"] = user
                        continue
                raise self._error(
                    ProviderErrorCategory.INVALID_INPUT,
                    f"{self._service_name} rejected the summary request "
                    f"(HTTP {response.status_code})",
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
                    input_tokens=max(
                        0,
                        int(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
                    ),
                    output_tokens=max(
                        0,
                        int(
                            usage.get(
                                "completion_tokens",
                                usage.get("output_tokens", 0),
                            )
                        ),
                    ),
                )
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise self._error(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    f"{self._service_name} returned an unexpected completion schema",
                    code="completion_schema",
                ) from exc
        raise AssertionError("unreachable retry loop")

    def _model_candidates(self) -> tuple[str, ...]:
        if self.active_model is None:
            return self.models
        return (
            self.active_model,
            *(model for model in self.models if model != self.active_model),
        )

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
        system: str,
        user: str,
        max_output_tokens: int,
        validator: Callable[[StructuredModel], bool] | None = None,
    ) -> tuple[StructuredModel, CompletionUsage]:
        """Generate and repair once with each configured model."""
        schema = json.dumps(
            model_type.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        total_usage = CompletionUsage()
        last_service_error: ProviderError | None = None
        last_validation_message: str | None = None
        last_validation_code: str | None = None
        fallback_categories = {
            ProviderErrorCategory.RATE_LIMITED,
            ProviderErrorCategory.UNAVAILABLE,
            ProviderErrorCategory.UNSUPPORTED,
        }
        for model in self._model_candidates():
            try:
                content, usage = self._complete(
                    model=model,
                    system=system,
                    user=user,
                    max_output_tokens=max_output_tokens,
                )
            except ProviderError as exc:
                if exc.failure.category not in fallback_categories:
                    raise
                last_service_error = exc
                continue
            total_usage += usage
            try:
                value = self._decode(content, model_type)
                if validator is not None and not validator(value):
                    raise ValueError("semantic JSON validation failed")
            except (json.JSONDecodeError, ValidationError, ValueError):
                try:
                    repaired, repair_usage = self._complete(
                        model=model,
                        system=system,
                        user=REPAIR_PROMPT.format(schema=schema, invalid=content),
                        max_output_tokens=max_output_tokens,
                    )
                except ProviderError as exc:
                    if exc.failure.category not in fallback_categories:
                        raise
                    last_service_error = exc
                    continue
                total_usage += repair_usage
                try:
                    value = self._decode(repaired, model_type)
                except (json.JSONDecodeError, ValidationError):
                    last_validation_message = (
                        f"{self._service_name} JSON repair did not satisfy the summary schema"
                    )
                    last_validation_code = "summary_schema"
                    continue
                if validator is not None and not validator(value):
                    last_validation_message = (
                        f"{self._service_name} JSON repair did not satisfy timeline constraints"
                    )
                    last_validation_code = "timeline_constraints"
                    continue
            self.active_model = model
            return value, total_usage
        if last_validation_message is not None:
            raise self._error(
                ProviderErrorCategory.SCHEMA_CHANGED,
                last_validation_message,
                code=last_validation_code,
            )
        if last_service_error is not None:
            raise last_service_error
        raise self._error(
            ProviderErrorCategory.UNAVAILABLE,
            f"No {self._service_name} summary model is available",
        )
