from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from xyz2notion.config import (
    AppConfig,
    ConfigurationError,
    MissingCredentialError,
    RuntimeCredentials,
    SummaryConfig,
)
from xyz2notion.models import ProviderError, ProviderErrorCategory, ProviderFailure
from xyz2notion.notion.client import NotionAPIError
from xyz2notion.orchestration import enrichment_queue as queue_module
from xyz2notion.orchestration.enrichment_queue import (
    BACKLOG_LIMIT,
    NORMAL_LIMIT,
    RETRY_LIMIT,
    EnrichmentQueueResult,
    process_enrichment_pass,
    resolve_queue_limit,
    select_enrichment_work,
)
from xyz2notion.orchestration.processor import EpisodeCandidate, ProcessingOutcome
from xyz2notion.state import PipelineState


def episode(
    page_id: str,
    *,
    status: str,
    played_seconds: int = 120,
    favorited: bool = False,
    liked: bool = False,
    listening_status: str | None = None,
    enrichment_status: str | None = None,
) -> dict[str, object]:
    page: dict[str, object] = {
        "id": page_id,
        "properties": {
            "Name": {"title": [{"plain_text": f"title-{page_id}"}]},
            "EID": {"rich_text": [{"plain_text": f"eid-{page_id}"}]},
            "Audio URL": {"url": f"https://audio.example/{page_id}.mp3"},
            "ASR Status": {"select": {"name": status}},
            "Played Seconds": {"number": played_seconds},
            "Favorited": {"checkbox": favorited},
            "Liked": {"checkbox": liked},
            "Listening Status": {
                "select": {"name": listening_status or "听过"},
            },
            "Skip AI": {"checkbox": False},
        },
    }
    if enrichment_status is not None:
        properties = page["properties"]
        assert isinstance(properties, dict)
        properties["增强状态"] = {"select": {"name": enrichment_status}}
    return page


@dataclass
class FakeProcessor:
    calls: list[str] = field(default_factory=list)

    def process(
        self,
        candidate: EpisodeCandidate,
        _page: object,
        *,
        retry_failed: bool = False,
        only_failed: bool = False,
    ) -> ProcessingOutcome:
        assert retry_failed is False
        assert only_failed is False
        self.calls.append(candidate.page_id)
        return ProcessingOutcome(candidate.eid, "updated", PipelineState.PUBLISHED)


class FakeContext:
    def __init__(self, value: object | None = None) -> None:
        self.value = self if value is None else value

    def __enter__(self) -> object:
        return self.value

    def __exit__(self, *_args: object) -> None:
        return None


class FakeNotion(FakeContext):
    def __init__(self, pages: list[dict[str, object]]) -> None:
        super().__init__()
        self.pages = pages
        self.queries: list[tuple[str, dict[str, int]]] = []

    def query_data_source(
        self,
        data_source_id: str,
        payload: dict[str, int],
    ) -> list[dict[str, object]]:
        self.queries.append((data_source_id, payload))
        return self.pages


def test_queue_selects_only_transcript_or_publish_checkpoints() -> None:
    pages = [
        episode("new", status="待处理"),
        episode("queued", status="排队中"),
        episode("running", status="转写中"),
        episode("transcribed", status="已转写"),
        episode("enriched", status="已增强"),
        episode("published", status="已发布"),
        episode("failed", status="可重试失败"),
    ]
    selected = select_enrichment_work(pages, limit=10)
    assert [candidate.page_id for candidate, _page in selected] == [
        "enriched",
        "transcribed",
    ]


def test_queue_uses_independent_enrichment_status_after_asr_is_complete() -> None:
    pages = [
        episode("pending", status="已转写", enrichment_status="待增强"),
        episode("publish", status="已转写", enrichment_status="待发布"),
        episode("complete", status="已转写", enrichment_status="已完成"),
        episode("failed", status="已转写", enrichment_status="最终失败"),
    ]
    selected = select_enrichment_work(pages, limit=10)
    assert [candidate.page_id for candidate, _page in selected] == ["publish", "pending"]


def test_queue_keeps_episode_candidate_safety_gates() -> None:
    pages = [
        episode("too-short", status="已转写", played_seconds=119),
        episode(
            "favorite",
            status="已转写",
            played_seconds=0,
            favorited=True,
        ),
    ]
    selected = select_enrichment_work(pages, limit=10)
    assert [candidate.page_id for candidate, _page in selected] == ["favorite"]


def test_enrichment_queue_applies_user_category_priority() -> None:
    pages = [
        episode("heard", status="已转写", listening_status="听过"),
        episode("liked", status="已转写", liked=True),
        episode("favorite", status="已增强", favorited=True),
    ]
    selected = select_enrichment_work(pages, limit=10)
    assert [candidate.page_id for candidate, _page in selected] == [
        "favorite",
        "liked",
        "heard",
    ]


def test_enrichment_pass_is_bounded_and_reports_only_aggregates() -> None:
    pages = [
        episode("b", status="已转写"),
        episode("a", status="已转写"),
        episode("c", status="已转写"),
    ]
    processor = FakeProcessor()
    result = process_enrichment_pass(pages, processor, limit=2)
    assert processor.calls == ["a", "b"]
    assert result.selected == 2
    assert result.remaining == 1
    assert result.actions == {"updated": 2}
    assert result.states == {"PUBLISHED": 2}
    assert result.categories == {"heard": 2}
    assert "eid-" not in result.summary()
    assert "title-" not in result.summary()


def test_enrichment_pass_retries_only_explicit_stage_safe_rows() -> None:
    pages = [
        episode("retryable", status="可重试失败"),
        episode("fresh", status="已转写"),
    ]

    @dataclass
    class RetryProcessor:
        calls: list[tuple[str, bool]] = field(default_factory=list)

        def process(
            self,
            candidate: EpisodeCandidate,
            _page: object,
            *,
            retry_failed: bool = False,
            only_failed: bool = False,
        ) -> ProcessingOutcome:
            assert only_failed is False
            self.calls.append((candidate.page_id, retry_failed))
            return ProcessingOutcome(candidate.eid, "updated", PipelineState.PUBLISHED)

    processor = RetryProcessor()
    result = process_enrichment_pass(
        pages,
        processor,
        limit=1,
        retryable_page_ids=("retryable",),
    )
    assert processor.calls == [("retryable", True)]
    assert result.selected == 1
    assert result.remaining == 1


def test_retry_mode_excludes_normal_transcripts() -> None:
    pages = [
        episode("retryable", status="可重试失败"),
        episode("fresh", status="已转写"),
    ]
    selected = select_enrichment_work(
        pages,
        limit=10,
        retryable_page_ids=("retryable",),
        only_retryable=True,
    )
    assert [candidate.page_id for candidate, _page in selected] == ["retryable"]


def test_retry_queue_mode_has_same_safe_cap() -> None:
    assert resolve_queue_limit("retry") == RETRY_LIMIT == 2
    assert resolve_queue_limit("retry", 99) == RETRY_LIMIT


def test_queue_modes_apply_strict_caps() -> None:
    assert resolve_queue_limit("backlog") == BACKLOG_LIMIT == 2
    assert resolve_queue_limit("backlog", 99) == 2
    assert resolve_queue_limit("normal") == NORMAL_LIMIT == 2
    assert resolve_queue_limit("normal", 2) == 2
    assert resolve_queue_limit("normal", 99) == 2
    with pytest.raises(ValueError, match="positive"):
        resolve_queue_limit("backlog", 0)
    with pytest.raises(ValueError, match="unknown"):
        resolve_queue_limit("invalid")


def test_status_parser_and_candidate_skip_malformed_pages() -> None:
    assert queue_module._episode_status({}) == ""  # type: ignore[attr-defined]
    assert queue_module._episode_status({"properties": []}) == ""  # type: ignore[attr-defined]
    assert (
        queue_module._episode_status(  # type: ignore[attr-defined]
            {"properties": {"ASR Status": []}}
        )
        == ""
    )
    assert queue_module._episode_enrichment_status({}) == ""  # type: ignore[attr-defined]
    assert (  # type: ignore[attr-defined]
        queue_module._episode_enrichment_status({"properties": {"增强状态": {"select": []}}}) == ""
    )
    assert (
        queue_module._episode_status(  # type: ignore[attr-defined]
            {"properties": {"ASR Status": {"select": []}}}
        )
        == ""
    )
    malformed = episode("missing-id", status="已转写")
    malformed.pop("id")
    assert select_enrichment_work([malformed], limit=2) == ()


def test_summary_policy_and_remote_to_local_client_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig()
    policy = queue_module._summary_policy(config)  # type: ignore[attr-defined]
    assert policy.prompt_version == config.summary.prompt_version
    assert policy.chunk_tokens == config.summary.chunk_tokens
    assert policy.chunk_minutes == config.summary.chunk_minutes
    assert policy.max_output_tokens == config.summary.max_output_tokens

    calls: list[tuple[str, object]] = []

    def remote(api_key: SecretStr, *, models: tuple[str, ...]) -> str:
        calls.append(("remote", (api_key.get_secret_value(), models)))
        return "remote-client"

    def local() -> str:
        calls.append(("local", None))
        return "local-client"

    def fallback(primary: object, secondary: object) -> tuple[object, object]:
        calls.append(("fallback", (primary, secondary)))
        return primary, secondary

    monkeypatch.setattr(queue_module, "SiliconFlowSummaryClient", remote)
    monkeypatch.setattr(queue_module, "LocalQwenSummaryClient", local)
    monkeypatch.setattr(queue_module, "FallbackSummaryClient", fallback)
    secret = SecretStr("safe-test-key")
    assert queue_module._summary_client(config, secret) == (  # type: ignore[attr-defined]
        "remote-client",
        "local-client",
    )
    assert calls == [
        ("remote", ("safe-test-key", ("Qwen/Qwen3-8B",))),
        ("local", None),
        ("fallback", ("remote-client", "local-client")),
    ]
    calls.clear()
    assert queue_module._summary_client(config, None) == (  # type: ignore[attr-defined]
        None,
        "local-client",
    )
    assert calls == [
        ("local", None),
        ("fallback", (None, "local-client")),
    ]


def test_summary_preflight_is_notion_free_and_validates_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = RuntimeCredentials(xiaoyuzhou_device_id="test-device")
    summary_context = FakeContext("summary-client")
    report = SimpleNamespace(summary=lambda: "healthy")
    calls: list[object] = []
    monkeypatch.setattr(queue_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(queue_module, "load_runtime_credentials", lambda: credentials)
    monkeypatch.setattr(queue_module, "_summary_client", lambda *_args: summary_context)
    monkeypatch.setattr(
        queue_module,
        "preflight_summary_client",
        lambda client: calls.append(client) or report,
    )
    assert queue_module.run_summary_preflight(config_path="config.yaml") is report
    assert calls == ["summary-client"]

    for config, message in (
        (AppConfig(summary=SummaryConfig(enabled=False)), "summary.enabled"),
        (
            AppConfig(summary=SummaryConfig(local_qwen_fallback=False)),
            "local_qwen_fallback",
        ),
    ):
        monkeypatch.setattr(queue_module, "load_config", lambda _path, value=config: value)
        with pytest.raises(ConfigurationError, match=message):
            queue_module.run_summary_preflight(config_path="config.yaml")


def test_run_enrichment_queue_uses_only_summary_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig()
    credentials = RuntimeCredentials(
        xiaoyuzhou_device_id="test-device",
        notion_token=SecretStr("notion-test"),
        notion_page_id="root-page",
        siliconflow_api_key=SecretStr("sf-test"),
    )
    pages = [episode("transcribed", status="已转写")]
    notion = FakeNotion(pages)
    summary_context = FakeContext("summary-client")
    state_context = FakeContext("state-store")
    constructed: dict[str, Any] = {}

    class FakeInitializer:
        def __init__(self, api: object, page_id: str) -> None:
            assert api is notion
            assert page_id == "root-page"

        def initialize(self) -> object:
            return SimpleNamespace(
                resources={
                    "episode": SimpleNamespace(data_source_id="episode-ds"),
                    "mindmap": SimpleNamespace(data_source_id="mindmap-ds"),
                }
            )

    class Processor(FakeProcessor):
        def __init__(self, api: object, store: object, **kwargs: object) -> None:
            super().__init__()
            assert api is notion
            assert store == "state-store"
            constructed.update(kwargs)

    monkeypatch.setattr(queue_module, "load_config", lambda _path: config)
    monkeypatch.setattr(queue_module, "load_runtime_credentials", lambda: credentials)
    monkeypatch.setattr(queue_module, "NotionClient", lambda _token: notion)
    monkeypatch.setattr(queue_module, "NotionInitializer", FakeInitializer)
    monkeypatch.setattr(queue_module, "_summary_client", lambda *_args: summary_context)
    monkeypatch.setattr(queue_module, "NotionEpisodeStateStore", lambda _api: state_context)
    monkeypatch.setattr(queue_module, "EpisodeAIProcessor", Processor)
    preflights: list[object] = []
    monkeypatch.setattr(
        queue_module,
        "preflight_summary_client",
        lambda client: preflights.append(client),
    )

    result = queue_module.run_enrichment_queue(
        config_path="config.yaml",
        mode="backlog",
        requested_limit=2,
    )
    assert result.selected == 1
    assert result.states == {"PUBLISHED": 1}
    assert notion.queries == [("episode-ds", {"page_size": 100})]
    assert constructed["siliconflow"] is None
    assert constructed["local_whisper"] is None
    assert constructed["summary_client"] == "summary-client"
    assert constructed["mindmap_data_source_id"] == "mindmap-ds"
    assert preflights == ["summary-client"]


@pytest.mark.parametrize(
    ("config", "credentials", "message"),
    [
        (
            AppConfig(summary=SummaryConfig(enabled=False)),
            None,
            "summary.enabled",
        ),
        (
            AppConfig(summary=SummaryConfig(local_qwen_fallback=False)),
            None,
            "local_qwen_fallback",
        ),
        (
            AppConfig(),
            RuntimeCredentials(
                xiaoyuzhou_device_id="test-device",
                notion_token=SecretStr("notion-test"),
            ),
            "Missing target page",
        ),
    ],
)
def test_run_enrichment_queue_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    credentials: RuntimeCredentials | None,
    message: str,
) -> None:
    monkeypatch.setattr(queue_module, "load_config", lambda _path: config)
    if credentials is not None:
        monkeypatch.setattr(queue_module, "load_runtime_credentials", lambda: credentials)
    with pytest.raises((ConfigurationError, MissingCredentialError), match=message):
        queue_module.run_enrichment_queue(config_path="config.yaml", mode="normal")


def test_run_enrichment_queue_requires_notion_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = RuntimeCredentials(
        xiaoyuzhou_device_id="test-device",
        notion_page_id="root-page",
    )
    monkeypatch.setattr(queue_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(queue_module, "load_runtime_credentials", lambda: credentials)
    with pytest.raises(MissingCredentialError, match="notion_token"):
        queue_module.run_enrichment_queue(config_path="config.yaml", mode="normal")


def test_main_success_and_safe_failure_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = EnrichmentQueueResult(
        selected=1,
        remaining=0,
        actions={"updated": 1},
        states={"PUBLISHED": 1},
    )
    monkeypatch.setattr(queue_module, "run_enrichment_queue", lambda **_kwargs: result)
    assert queue_module.main(["--mode", "backlog", "--limit", "2"]) == 0
    assert "selected=1" in capsys.readouterr().out

    failed = EnrichmentQueueResult(
        selected=2,
        remaining=0,
        actions={"failed": 2},
        states={"FAILED_RETRYABLE": 2},
        failure_categories={"unavailable": 2},
    )
    monkeypatch.setattr(queue_module, "run_enrichment_queue", lambda **_kwargs: failed)
    assert queue_module.main([]) == 5
    failed_output = capsys.readouterr().out
    assert "Transcript enrichment FAILED" in failed_output
    assert "failure_categories: unavailable=2" in failed_output

    preflight = SimpleNamespace(summary=lambda: "Summary route preflight OK")
    monkeypatch.setattr(queue_module, "run_summary_preflight", lambda **_kwargs: preflight)
    assert queue_module.main(["--preflight-only"]) == 0
    assert capsys.readouterr().out.strip() == "Summary route preflight OK"

    def config_failure(**_kwargs: object) -> EnrichmentQueueResult:
        raise ConfigurationError("bad config")

    monkeypatch.setattr(queue_module, "run_enrichment_queue", config_failure)
    assert queue_module.main([]) == 2
    assert "Configuration error: bad config" in capsys.readouterr().err

    def notion_failure(**_kwargs: object) -> EnrichmentQueueResult:
        raise NotionAPIError("safe notion failure")

    monkeypatch.setattr(queue_module, "run_enrichment_queue", notion_failure)
    assert queue_module.main([]) == 4
    assert "Notion error: safe notion failure" in capsys.readouterr().err

    def provider_failure(**_kwargs: object) -> EnrichmentQueueResult:
        raise ProviderError(
            ProviderFailure(
                provider="summary_fallback_chain",
                category=ProviderErrorCategory.UNAVAILABLE,
                message="safe combined failure",
                code="runtime_load",
            )
        )

    monkeypatch.setattr(queue_module, "run_enrichment_queue", provider_failure)
    assert queue_module.main([]) == 5
    provider_error = capsys.readouterr().err
    assert "provider=summary_fallback_chain" in provider_error
    assert "category=unavailable; code=runtime_load" in provider_error
    assert "detail=safe combined failure" in provider_error


def test_enrichment_workflow_is_asr_free_cached_and_mode_gated() -> None:
    path = Path(".github/workflows/enrich-transcripts.yml")
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["group"] == "xyz2notion-runtime"
    assert workflow[True]["schedule"] == [
        {"cron": "41 */2 * * *"},
    ]
    assert workflow[True]["workflow_run"] == {
        "workflows": ["Transcribe Episode Queue", "Retry Failed Episode AI"],
        "types": ["completed"],
    }
    assert "XYZ2NOTION_ENRICHMENT_QUEUE_ENABLED" in text
    assert "XYZ2NOTION_ENRICHMENT_BACKLOG" in text
    assert "BACKLOG_ENABLED" in text
    assert "github.event_name == 'schedule'" in text
    assert "TINGWU_COOKIE" not in text
    assert "XIAOYUZHOU_REFRESH_TOKEN" not in text
    assert "ffmpeg" not in text.lower()
    assert "process-ai" not in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "37 22 * * *" not in text
    assert "xyz2notion.orchestration.enrichment_queue" in text
    assert "preflight_only" in text
    assert "--preflight-only" in text
    assert 'exit "$status"' in text
    assert "~/.cache/xyz2notion" in text
    assert "llama_cpp_python-0.3.19-cp312-cp312-linux_x86_64.whl" in text
