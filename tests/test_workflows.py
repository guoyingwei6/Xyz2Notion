from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
PINNED_CHECKOUT = "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
PINNED_SETUP = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
PINNED_CACHE = "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830"


def _workflow(name: str) -> tuple[str, dict[str, object]]:
    text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return text, parsed


def test_all_workflows_are_read_only_pinned_and_bounded() -> None:
    for path in WORKFLOW_DIR.glob("*.yml"):
        text, workflow = _workflow(path.name)
        assert workflow["permissions"] == {"contents": "read"}
        assert "timeout-minutes:" in text
        assert PINNED_CHECKOUT in text
        assert PINNED_SETUP in text or (path.name == "ci.yml" and PINNED_SETUP in text)
        assert "actions/upload-artifact" not in text
        assert "pull_request_target" not in text


def test_runtime_workflows_use_concurrency_and_private_safe_summaries() -> None:
    for name in (
        "init-notion.yml",
        "sync-metadata.yml",
        "transcribe-asr.yml",
        "enrich-transcripts.yml",
        "retry-failed-ai.yml",
        "maintenance.yml",
    ):
        text, workflow = _workflow(name)
        assert workflow["concurrency"]["cancel-in-progress"] is False  # type: ignore[index]
        assert "GITHUB_STEP_SUMMARY" in text
        assert "uv run xyz2notion" in text or "uv run python -m xyz2notion" in text
        assert "upload-artifact" not in text


def test_init_workflow_requires_exact_dashboard_rebuild_confirmation() -> None:
    text, workflow = _workflow("init-notion.yml")
    dispatch = workflow[True]["workflow_dispatch"]  # type: ignore[index,operator]
    inputs = dispatch["inputs"]  # type: ignore[index]
    assert inputs["operation"]["default"] == "initialize"  # type: ignore[index]
    assert inputs["operation"]["options"] == [  # type: ignore[index]
        "bootstrap",
        "initialize",
        "audit-dashboard",
        "audit-view-configurations",
        "cleanup-dashboard-layout",
        "rebuild-dashboard",
        "rebuild-dashboard-layout",
    ]
    assert inputs["confirmation"]["required"] is False  # type: ignore[index]
    assert inputs["expected_count"]["required"] is False  # type: ignore[index]
    assert inputs["expected_total"]["required"] is False  # type: ignore[index]
    assert "default" not in inputs["confirmation"]  # type: ignore[operator]
    assert "default" not in inputs["expected_count"]  # type: ignore[operator]
    assert "default" not in inputs["expected_total"]  # type: ignore[operator]
    assert 'OPERATION" == "initialize"' in text
    assert "uv run xyz2notion notion-init" in text
    assert 'OPERATION" == "bootstrap"' in text
    assert "uv run xyz2notion notion-init --create-home" in text
    assert 'OPERATION" == "audit-dashboard"' in text
    assert "uv run xyz2notion audit-dashboard" in text
    assert 'OPERATION" == "audit-view-configurations"' in text
    assert "uv run xyz2notion audit-view-configurations --details" in text
    assert 'OPERATION" == "cleanup-dashboard-layout"' in text
    assert "ARCHIVE_${EXPECTED_COUNT}_BUNDLES_${expected_blocks}_LAYOUT_BLOCKS" in text
    assert "uv run xyz2notion cleanup-dashboard-layout" in text
    assert '--expected-bundles "$EXPECTED_COUNT"' in text
    assert '--expected-total "$EXPECTED_TOTAL"' in text
    assert 'OPERATION" == "rebuild-dashboard"' in text
    assert 'EXPECTED_COUNT" =~ ^[1-9][0-9]*$' in text
    assert 'CONFIRMATION" != "ARCHIVE_${EXPECTED_COUNT}_LINKED_DATABASE_BLOCKS"' in text
    assert "rebuild-dashboard" in text
    assert '--confirm "$CONFIRMATION"' in text
    assert '--expected-count "$EXPECTED_COUNT"' in text
    assert 'OPERATION" == "rebuild-dashboard-layout"' in text
    assert "REBUILD_MANAGED_DASHBOARD_LAYOUT_${EXPECTED_TOTAL}_BLOCKS" in text
    assert "uv run xyz2notion rebuild-dashboard-layout" in text
    assert '--expected-total "$EXPECTED_TOTAL"' in text


def test_metadata_sync_runs_daily_and_manual_runs_require_confirmation() -> None:
    text, workflow = _workflow("sync-metadata.yml")
    assert workflow[True]["schedule"] == [{"cron": "17 21 * * *"}]  # type: ignore[index]
    dispatch = workflow[True]["workflow_dispatch"]  # type: ignore[index,operator]
    assert dispatch["inputs"]["confirmation"]["required"] is True  # type: ignore[index]
    assert "RUN_SAFE_INCREMENTAL_SYNC" in text
    assert 'EVENT_NAME" == "workflow_dispatch"' in text
    assert 'EVENT_NAME" != "workflow_dispatch"' in text
    assert 'EVENT_NAME" != "schedule"' in text
    assert "uv run xyz2notion repair-notion-covers" in text
    assert "--limit 10 --confirm REPAIR_10_NOTION_COVERS" in text
    assert "timeout-minutes: 15" in text


def test_transcribe_workflow_uses_only_current_asr_providers() -> None:
    text, workflow = _workflow("transcribe-asr.yml")
    assert workflow[True]["schedule"] == [  # type: ignore[index]
        {"cron": "13 */2 * * *"},
    ]
    assert workflow[True]["workflow_run"] == {  # type: ignore[index]
        "workflows": ["Sync Podcast Metadata"],
        "types": ["completed"],
    }
    assert "DASHSCOPE_API_KEY" in text
    assert "SILICONFLOW_API_KEY" in text
    assert "TINGWU_COOKIE" not in text
    assert "process-asr" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event_name == 'workflow_run'" in text
    assert "47 21 * * *" not in text


def test_enrichment_workflow_uses_only_summary_credentials() -> None:
    text, workflow = _workflow("enrich-transcripts.yml")
    assert workflow[True]["schedule"] == [  # type: ignore[index]
        {"cron": "41 */2 * * *"},
    ]
    assert workflow[True]["workflow_run"] == {  # type: ignore[index]
        "workflows": ["Transcribe Episode Queue"],
        "types": ["completed"],
    }
    assert "SILICONFLOW_API_KEY" in text
    assert "TINGWU_COOKIE" not in text
    assert "process-ai" not in text
    assert "llama_cpp_python-0.3.35-py3-none-manylinux2014_x86_64" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event_name == 'workflow_run'" in text
    assert "xyz2notion.orchestration.summary_diagnostic" in text
    assert "37 22 * * *" not in text


def test_retry_failed_ai_workflow_is_bounded_and_retry_only() -> None:
    text, workflow = _workflow("retry-failed-ai.yml")
    assert workflow[True]["schedule"] == [{"cron": "53 */2 * * *"}]  # type: ignore[index]
    assert "--mode retry" in text
    assert "Retry failed ASR checkpoints only" in text
    assert "Retry failed enrichment checkpoints only" in text
    assert "Process manually requested retries first" in text
    assert "process-manual-retries" in text
    assert "github.event_name == 'schedule'" in text
    assert "vars.ASR_QUEUE_ENABLED == 'true'" in text
    assert "vars.XYZ2NOTION_ENRICHMENT_QUEUE_ENABLED == 'true'" in text
    assert "vars.ASR_QUEUE_ENABLED == 'true'" in text
    assert "XYZ2NOTION_ENRICHMENT_QUEUE_ENABLED" in text
    assert "group: xyz2notion-runtime" in text
    assert "TINGWU_COOKIE" not in text
    assert "XIAOYUZHOU_REFRESH_TOKEN" not in text


def test_maintenance_workflow_has_safe_dispatch_guards() -> None:
    text, workflow = _workflow("maintenance.yml")
    assert workflow["permissions"] == {"contents": "read"}
    for operation in (
        "migrate-dry-run",
        "migrate",
        "redo-episode",
        "rebuild-statistics",
        "rebuild-heatmap",
    ):
        assert operation in text
    assert "confirm_changes" in text
    assert 'CONFIRM_CHANGES" != "true' in text
    assert "NOTION_MIGRATION_PAGE_ID" in text
    assert "XIAOYUZHOU_REFRESH_TOKEN" not in text
    assert "XIAOYUZHOU_DEVICE_ID" not in text


def test_all_notion_writers_share_one_concurrency_group() -> None:
    for name in (
        "init-notion.yml",
        "sync-metadata.yml",
        "transcribe-asr.yml",
        "enrich-transcripts.yml",
        "maintenance.yml",
        "notion-repair.yml",
    ):
        text, _workflow_data = _workflow(name)
        assert "group: xyz2notion-runtime" in text


def test_notion_repair_supports_read_only_backlog_audit() -> None:
    text, workflow = _workflow("notion-repair.yml")
    dispatch = workflow[True]["workflow_dispatch"]  # type: ignore[index,operator]
    options = dispatch["inputs"]["operation"]["options"]  # type: ignore[index]
    assert "audit-backlog" in options
    assert "uv run xyz2notion audit-notion-backlog" in text
    assert "reopen-timeline-failures" in options
    assert "uv run xyz2notion reopen-timeline-failures" in text
    assert "reopen-summary-failures" in options
    assert "uv run xyz2notion reopen-summary-failures" in text
    assert "archive-legacy-zero-play" in options
    assert "uv run xyz2notion archive-legacy-zero-play" in text
    assert '--expected-count "$LIMIT"' in text
    assert "XIAOYUZHOU_REFRESH_TOKEN" not in text


def test_final_summary_recovery_is_bounded_audited_and_asr_free() -> None:
    text, workflow = _workflow("recover-final-summaries.yml")
    dispatch = workflow[True]["workflow_dispatch"]  # type: ignore[index,operator]
    inputs = dispatch["inputs"]  # type: ignore[index]
    assert inputs["batches"]["options"] == ["1", "2", "3", "4", "5", "6"]  # type: ignore[index]
    assert "timeout-minutes: 360" in text
    assert "reopen-summary-failures" in text
    assert "--limit 2 --confirm REOPEN_2_SUMMARY_FAILURES" in text
    assert "process-manual-retries" in text
    assert "audit-notion-backlog" in text
    assert "retry_status" in text
    assert "process-asr" not in text
