import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from xyz2notion.orchestration import summary_diagnostic
from xyz2notion.orchestration.summary_diagnostic import (
    SILICONFLOW_MODELS_URL,
    DiagnosticProbe,
    SiliconFlowSummaryDiagnostic,
    diagnose_siliconflow_summary,
)

API_KEY = "diagnostic-fixture-secret"
MODEL = "Qwen/Qwen3-8B"


def test_diagnostic_is_secret_safe_and_isolates_json_mode() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        if str(request.url) == SILICONFLOW_MODELS_URL:
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, json={"code": 30001, "message": API_KEY})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    result = diagnose_siliconflow_summary(API_KEY, client=client)

    assert result.model_listed is True
    assert result.minimal_accepted is True
    assert [probe.accepted for probe in result.probes] == [True, True, True, False]
    assert result.probes[-1].code == "30001"
    assert API_KEY not in result.summary()


def test_diagnostic_identifies_invalid_key_without_leaking_body() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                401,
                json={"error": {"code": "InvalidApiKey", "message": API_KEY}},
            )
        )
    )
    result = diagnose_siliconflow_summary(API_KEY, client=client)

    assert result.model_listed is None
    assert result.minimal_accepted is False
    assert all(probe.status == "401" for probe in result.probes)
    assert all(probe.code == "InvalidApiKey" for probe in result.probes)
    assert API_KEY not in result.summary()


def test_diagnostic_safely_handles_transport_and_malformed_responses() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(API_KEY, request=request)

    transport_result = diagnose_siliconflow_summary(
        SecretStr(API_KEY),
        client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    assert transport_result.model_listed is None
    assert all(probe.status == "transport_error" for probe in transport_result.probes)
    assert all(probe.code == "ConnectTimeout" for probe in transport_result.probes)
    assert API_KEY not in transport_result.summary()

    def malformed(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SILICONFLOW_MODELS_URL:
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json=[])

    malformed_result = diagnose_siliconflow_summary(
        API_KEY,
        client=httpx.Client(transport=httpx.MockTransport(malformed)),
    )
    assert malformed_result.model_listed is None
    assert malformed_result.minimal_accepted is True
    assert all(probe.code == "200" for probe in malformed_result.probes)

    with pytest.raises(ValueError, match="cannot be empty"):
        diagnose_siliconflow_summary(" ")


def test_diagnostic_main_reports_missing_and_live_route(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        summary_diagnostic,
        "load_runtime_credentials",
        lambda: SimpleNamespace(siliconflow_api_key=None),
    )
    assert summary_diagnostic.main() == 2
    assert "missing SILICONFLOW_API_KEY" in capsys.readouterr().err

    live = SiliconFlowSummaryDiagnostic(
        model=MODEL,
        model_listed=True,
        probes=(DiagnosticProbe("minimal", True, "200", "200"),),
    )
    monkeypatch.setattr(
        summary_diagnostic,
        "load_runtime_credentials",
        lambda: SimpleNamespace(siliconflow_api_key=SecretStr(API_KEY)),
    )
    monkeypatch.setattr(
        summary_diagnostic,
        "diagnose_siliconflow_summary",
        lambda _key, *, model: live,
    )
    assert summary_diagnostic.main() == 0
    assert "model_listed=true" in capsys.readouterr().out
