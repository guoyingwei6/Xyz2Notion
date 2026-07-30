from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
PINNED_CHECKOUT = "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
PINNED_SETUP = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"


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
        "process-ai.yml",
        "retry-failed.yml",
        "maintenance.yml",
    ):
        text, workflow = _workflow(name)
        assert workflow["concurrency"]["cancel-in-progress"] is False  # type: ignore[index]
        assert "GITHUB_STEP_SUMMARY" in text
        assert "uv run xyz2notion" in text
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


def test_metadata_sync_is_manual_and_requires_exact_safety_confirmation() -> None:
    text, workflow = _workflow("sync-metadata.yml")
    assert "cron:" not in text
    dispatch = workflow[True]["workflow_dispatch"]  # type: ignore[index,operator]
    assert dispatch["inputs"]["confirmation"]["required"] is True  # type: ignore[index]
    assert "RUN_SAFE_INCREMENTAL_SYNC" in text
    assert "timeout-minutes: 15" in text


def test_process_ai_runs_two_hour_drain_schedule() -> None:
    text, workflow = _workflow("process-ai.yml")
    assert "workflow_dispatch" in workflow[True]  # type: ignore[index,operator]
    assert workflow[True]["schedule"] == [{"cron": "23 */2 * * *"}]  # type: ignore[index]


def test_retry_failed_runs_once_daily() -> None:
    _text, workflow = _workflow("retry-failed.yml")
    assert "workflow_dispatch" in workflow[True]  # type: ignore[index,operator]
    assert workflow[True]["schedule"] == [{"cron": "47 2 * * *"}]  # type: ignore[index]


def test_ai_workflows_receive_only_expected_provider_secrets() -> None:
    for name in ("process-ai.yml", "retry-failed.yml"):
        text, _workflow_data = _workflow(name)
        for secret in (
            "NOTION_TOKEN",
            "NOTION_PAGE_ID",
            "TINGWU_COOKIE",
            "SILICONFLOW_API_KEY",
        ):
            assert f"secrets.{secret}" in text
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


def test_all_notion_writers_share_one_concurrency_group() -> None:
    for name in (
        "init-notion.yml",
        "sync-metadata.yml",
        "process-ai.yml",
        "retry-failed.yml",
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
    assert "XIAOYUZHOU_REFRESH_TOKEN" not in text
