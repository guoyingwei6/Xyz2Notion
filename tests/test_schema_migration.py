import pytest

from xyz2notion.migration.schema import (
    CURRENT_WORKSPACE_SCHEMA_VERSION,
    detect_workspace_schema_version,
    migration_plan,
)
from xyz2notion.notion.client import rich_text


def test_legacy_workspace_plans_additive_bootstrap() -> None:
    plan = migration_plan(detect_workspace_schema_version([]))
    assert [(step.from_version, step.to_version) for step in plan] == [(0, 1)]


def test_current_marker_requires_no_migration() -> None:
    blocks = [
        {
            "type": "callout",
            "callout": {
                "rich_text": rich_text("XYZ2NOTION_MANAGED_HOME_V1"),
            },
        }
    ]
    assert detect_workspace_schema_version(blocks) == CURRENT_WORKSPACE_SCHEMA_VERSION
    assert migration_plan(CURRENT_WORKSPACE_SCHEMA_VERSION) == ()


def test_marker_parser_ignores_unrelated_and_supports_text_content() -> None:
    blocks = [
        {"type": "divider", "divider": {}},
        {"type": "paragraph", "paragraph": {"rich_text": "invalid"}},
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    None,
                    {"text": {"content": "XYZ2NOTION_MANAGED_HOME_V1"}},
                ]
            },
        },
    ]
    assert detect_workspace_schema_version(blocks) == 1


def test_newer_workspace_is_rejected_instead_of_downgraded() -> None:
    with pytest.raises(ValueError, match="newer"):
        migration_plan(CURRENT_WORKSPACE_SCHEMA_VERSION + 1)


def test_negative_workspace_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        migration_plan(-1)
