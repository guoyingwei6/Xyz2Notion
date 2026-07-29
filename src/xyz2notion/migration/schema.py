"""Version contract for additive Notion workspace schema migrations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

CURRENT_WORKSPACE_SCHEMA_VERSION = 1
SCHEMA_MARKER_PATTERN = re.compile(r"XYZ2NOTION_MANAGED_HOME_V(?P<version>\d+)")


@dataclass(frozen=True)
class SchemaMigrationStep:
    from_version: int
    to_version: int
    name: str


MIGRATIONS = (SchemaMigrationStep(0, 1, "bootstrap-additive-clean-room-schema"),)


def _block_text(block: Mapping[str, object]) -> str:
    block_type = block.get("type")
    body = block.get(str(block_type))
    if not isinstance(body, Mapping):
        return ""
    rich_text = body.get("rich_text")
    if not isinstance(rich_text, list):
        return ""
    parts: list[str] = []
    for item in rich_text:
        if not isinstance(item, Mapping):
            continue
        if item.get("plain_text") is not None:
            parts.append(str(item["plain_text"]))
            continue
        text = item.get("text")
        if isinstance(text, Mapping) and text.get("content") is not None:
            parts.append(str(text["content"]))
    return "".join(parts)


def detect_workspace_schema_version(blocks: Sequence[Mapping[str, object]]) -> int:
    """Read the highest managed schema marker, treating legacy templates as v0."""
    versions = [
        int(match.group("version"))
        for block in blocks
        if (match := SCHEMA_MARKER_PATTERN.search(_block_text(block))) is not None
    ]
    return max(versions, default=0)


def migration_plan(from_version: int) -> tuple[SchemaMigrationStep, ...]:
    """Return a contiguous additive migration path to the current version."""
    if from_version < 0:
        raise ValueError("workspace schema version cannot be negative")
    if from_version > CURRENT_WORKSPACE_SCHEMA_VERSION:
        raise ValueError("workspace schema is newer than this Xyz2Notion release")
    pending: list[SchemaMigrationStep] = []
    version = from_version
    for step in MIGRATIONS:
        if step.from_version == version:
            pending.append(step)
            version = step.to_version
    if version != CURRENT_WORKSPACE_SCHEMA_VERSION:
        raise ValueError("no contiguous workspace schema migration path")
    return tuple(pending)
