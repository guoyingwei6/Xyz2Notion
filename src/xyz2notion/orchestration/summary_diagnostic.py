"""Secret-safe live diagnostics for configured remote summary providers."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from xyz2notion.config import load_runtime_credentials
from xyz2notion.enrichment.siliconflow import (
    DEFAULT_SUMMARY_MODELS,
    SILICONFLOW_CHAT_URL,
)
from xyz2notion.security import CredentialKind, validate_credential_destination

SILICONFLOW_MODELS_URL = "https://api.siliconflow.cn/v1/models"
DASHSCOPE_CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DASHSCOPE_DIAGNOSTIC_MODEL = "qwen-flash"
DIAGNOSTIC_MODELS = (
    DEFAULT_SUMMARY_MODELS[0],
    "Qwen/Qwen2.5-7B-Instruct",
)


def _safe_code(value: object) -> str:
    return "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in str(value)
    )[:80]


def _response_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return str(response.status_code)
    if not isinstance(payload, Mapping):
        return str(response.status_code)
    error = payload.get("error")
    if isinstance(error, Mapping):
        value = error.get("code") or error.get("type")
    else:
        value = payload.get("code")
    return _safe_code(value if value is not None else response.status_code)


@dataclass(frozen=True)
class DiagnosticProbe:
    """One response summary that never contains provider response text."""

    name: str
    accepted: bool
    status: str
    code: str

    def summary(self) -> str:
        return (
            f"probe={self.name}; accepted={str(self.accepted).lower()}; "
            f"status={self.status}; code={self.code}"
        )


@dataclass(frozen=True)
class SiliconFlowSummaryDiagnostic:
    """Model visibility plus a bounded optional-parameter capability matrix."""

    model: str
    model_listed: bool | None
    probes: tuple[DiagnosticProbe, ...]

    @property
    def minimal_accepted(self) -> bool:
        return any(probe.name == "minimal" and probe.accepted for probe in self.probes)

    def summary(self) -> str:
        listed = "unknown" if self.model_listed is None else str(self.model_listed).lower()
        lines = [
            f"SiliconFlow summary diagnostic (model={self.model}; model_listed={listed})",
            *(probe.summary() for probe in self.probes),
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class DashScopeSummaryDiagnostic:
    """Bounded request-capability matrix for one DashScope text model."""

    model: str
    probes: tuple[DiagnosticProbe, ...]

    @property
    def minimal_accepted(self) -> bool:
        return any(probe.name == "minimal" and probe.accepted for probe in self.probes)

    def summary(self) -> str:
        lines = [
            f"DashScope summary diagnostic (model={self.model})",
            *(probe.summary() for probe in self.probes),
        ]
        return "\n".join(lines)


def _probe_request(
    client: httpx.Client,
    *,
    url: str,
    name: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
) -> DiagnosticProbe:
    try:
        response = client.post(url, headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return DiagnosticProbe(
            name=name,
            accepted=False,
            status="transport_error",
            code=_safe_code(type(exc).__name__),
        )
    return DiagnosticProbe(
        name=name,
        accepted=not response.is_error,
        status=str(response.status_code),
        code=_response_code(response),
    )


def diagnose_siliconflow_summary(
    api_key: str | SecretStr,
    *,
    model: str = DEFAULT_SUMMARY_MODELS[0],
    client: httpx.Client | None = None,
) -> SiliconFlowSummaryDiagnostic:
    """Test model visibility and request features without logging any response body."""
    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    if not secret.strip():
        raise ValueError("SiliconFlow API key cannot be empty")
    validate_credential_destination(SILICONFLOW_MODELS_URL, CredentialKind.SILICONFLOW)
    validate_credential_destination(SILICONFLOW_CHAT_URL, CredentialKind.SILICONFLOW)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    active_client = client or httpx.Client(timeout=90)
    try:
        model_listed: bool | None = None
        try:
            models_response = active_client.get(SILICONFLOW_MODELS_URL, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            models_probe = DiagnosticProbe(
                name="models",
                accepted=False,
                status="transport_error",
                code=_safe_code(type(exc).__name__),
            )
        else:
            models_probe = DiagnosticProbe(
                name="models",
                accepted=not models_response.is_error,
                status=str(models_response.status_code),
                code=_response_code(models_response),
            )
            if models_probe.accepted:
                try:
                    payload = models_response.json()
                    data = payload["data"]
                    model_listed = any(
                        isinstance(item, Mapping) and item.get("id") == model for item in data
                    )
                except (KeyError, TypeError, ValueError):
                    model_listed = None

        base: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": 'Return exactly {"ok":true}.'},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
        }
        no_thinking = {**base, "enable_thinking": False}
        json_no_thinking = {
            **no_thinking,
            "response_format": {"type": "json_object"},
        }
        probes = (
            models_probe,
            _probe_request(
                active_client,
                url=SILICONFLOW_CHAT_URL,
                name="minimal",
                headers=headers,
                payload=base,
            ),
            _probe_request(
                active_client,
                url=SILICONFLOW_CHAT_URL,
                name="no_thinking",
                headers=headers,
                payload=no_thinking,
            ),
            _probe_request(
                active_client,
                url=SILICONFLOW_CHAT_URL,
                name="json_no_thinking",
                headers=headers,
                payload=json_no_thinking,
            ),
        )
        return SiliconFlowSummaryDiagnostic(
            model=model,
            model_listed=model_listed,
            probes=probes,
        )
    finally:
        if owns_client:
            active_client.close()


def diagnose_dashscope_summary(
    api_key: str | SecretStr,
    *,
    model: str = DASHSCOPE_DIAGNOSTIC_MODEL,
    client: httpx.Client | None = None,
) -> DashScopeSummaryDiagnostic:
    """Probe DashScope chat and JSON modes without logging generated content."""
    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    if not secret.strip():
        raise ValueError("DashScope API key cannot be empty")
    validate_credential_destination(DASHSCOPE_CHAT_URL, CredentialKind.DASHSCOPE)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    active_client = client or httpx.Client(timeout=90)
    try:
        base: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": 'Return exactly {"ok":true}.'},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 256,
        }
        no_thinking = {**base, "enable_thinking": False}
        json_no_thinking = {
            **no_thinking,
            "response_format": {"type": "json_object"},
        }
        probes = tuple(
            _probe_request(
                active_client,
                url=DASHSCOPE_CHAT_URL,
                name=name,
                headers=headers,
                payload=payload,
            )
            for name, payload in (
                ("minimal", base),
                ("no_thinking", no_thinking),
                ("json_no_thinking", json_no_thinking),
            )
        )
        return DashScopeSummaryDiagnostic(model=model, probes=probes)
    finally:
        if owns_client:
            active_client.close()


def main(_argv: Sequence[str] | None = None) -> int:
    credentials = load_runtime_credentials()
    if credentials.siliconflow_api_key is None and credentials.dashscope_api_key is None:
        print(
            "Configuration error: missing SILICONFLOW_API_KEY and DASHSCOPE_API_KEY",
            file=sys.stderr,
        )
        return 2
    diagnostics: tuple[SiliconFlowSummaryDiagnostic | DashScopeSummaryDiagnostic, ...] = ()
    if credentials.siliconflow_api_key is not None:
        diagnostics += tuple(
            diagnose_siliconflow_summary(credentials.siliconflow_api_key, model=model)
            for model in DIAGNOSTIC_MODELS
        )
    if credentials.dashscope_api_key is not None:
        diagnostics += (diagnose_dashscope_summary(credentials.dashscope_api_key),)
    print("\n".join(diagnostic.summary() for diagnostic in diagnostics))
    return 0 if any(diagnostic.minimal_accepted for diagnostic in diagnostics) else 5


if __name__ == "__main__":
    raise SystemExit(main())
