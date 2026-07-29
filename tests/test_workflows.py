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
        "initialize",
        "audit-dashboard",
        "rebuild-dashboard",
    ]
    assert inputs["confirmation"]["required"] is False  # type: ignore[index]
    assert inputs["expected_count"]["required"] is False  # type: ignore[index]
    assert "default" not in inputs["confirmation"]  # type: ignore[operator]
    assert "default" not in inputs["expected_count"]  # type: ignore[operator]
    assert 'OPERATION" == "initialize"' in text
    assert "uv run xyz2notion notion-init" in text
    assert 'OPERATION" == "audit-dashboard"' in text
    assert "uv run xyz2notion audit-dashboard" in text
    assert 'OPERATION" == "rebuild-dashboard"' in text
    assert 'EXPECTED_COUNT" =~ ^[1-9][0-9]*$' in text
    assert 'CONFIRMATION" != "ARCHIVE_${EXPECTED_COUNT}_LINKED_DATABASE_BLOCKS"' in text
    assert "rebuild-dashboard" in text
    assert '--confirm "$CONFIRMATION"' in text
    assert '--expected-count "$EXPECTED_COUNT"' in text


def test_schedules_avoid_the_top_of_the_hour() -> None:
    text, _workflow_data = _workflow("sync-metadata.yml")
    cron_lines = [line.strip() for line in text.splitlines() if "cron:" in line]
    assert cron_lines
    assert all('cron: "0 ' not in line for line in cron_lines)


def test_process_ai_is_manual_during_initial_validation() -> None:
    text, workflow = _workflow("process-ai.yml")
    assert "cron:" not in text
    assert "workflow_dispatch" in workflow[True]  # type: ignore[index,operator]


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
    ):
        text, _workflow_data = _workflow(name)
        assert "group: xyz2notion-runtime" in text
