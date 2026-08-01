"""Map legacy Podcast2Notion properties in place without moving row pages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.schema import NotionResource

LEGACY_SERVICE_HOSTS = frozenset(
    {
        "heatmap.malinkang.com",
        "mindmap.malinkang.com",
        "notion-music.malinkang.com",
    }
)


class MigrationAPI(Protocol):
    def query_data_source(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...

    def list_block_children(self, block_id: str) -> list[JsonObject]: ...

    def delete_block(self, block_id: str) -> JsonObject: ...


@dataclass(frozen=True)
class PageMigration:
    resource: str
    page_id: str
    stable_key: str
    properties: JsonObject


@dataclass(frozen=True)
class MigrationPlan:
    scanned_pages: int
    changes: tuple[PageMigration, ...]
    legacy_embed_ids: tuple[str, ...]
    duplicate_keys: tuple[str, ...]


@dataclass(frozen=True)
class MigrationReport:
    scanned_pages: int
    planned_updates: int
    updated_pages: int
    legacy_embeds_found: int
    legacy_embeds_removed: int
    duplicate_keys: tuple[str, ...]
    dry_run: bool


def _text_items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _plain_text(value: object) -> str:
    parts: list[str] = []
    for item in _text_items(value):
        if not isinstance(item, Mapping):
            continue
        if item.get("plain_text") is not None:
            parts.append(str(item["plain_text"]))
            continue
        text = item.get("text")
        if isinstance(text, Mapping) and text.get("content") is not None:
            parts.append(str(text["content"]))
    return "".join(parts)


def _value(properties: Mapping[str, Any], *names: str) -> object | None:
    for name in names:
        prop = properties.get(name)
        if not isinstance(prop, Mapping):
            continue
        if "title" in prop:
            return _plain_text(prop["title"])
        if "rich_text" in prop:
            return _plain_text(prop["rich_text"])
        for key in ("number", "checkbox", "url", "relation"):
            if key in prop:
                return cast(object, prop[key])
        for key in ("select", "status", "date"):
            selected = prop.get(key)
            if isinstance(selected, Mapping):
                return selected.get("name") if key != "date" else dict(selected)
    return None


def _nonempty(properties: Mapping[str, Any], name: str) -> bool:
    value = _value(properties, name)
    return value not in (None, "", [], ())


def _title(value: object) -> JsonObject:
    return {"title": rich_text(str(value))}


def _text(value: object) -> JsonObject:
    return {"rich_text": rich_text(str(value))}


def _copy_date(value: object) -> JsonObject | None:
    if isinstance(value, Mapping) and value.get("start"):
        return {"date": dict(value)}
    return None


def _clean_url(value: object) -> str:
    rendered = str(value or "")
    return rendered[1:] if rendered.startswith("hhttps://") else rendered


def _legacy_task_id(value: object) -> str:
    path = urlparse(str(value or "")).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _mapped(
    properties: Mapping[str, Any],
    mapping: Mapping[str, tuple[str, ...]],
) -> JsonObject:
    result: JsonObject = {}
    for target, aliases in mapping.items():
        if _nonempty(properties, target):
            continue
        value = _value(properties, *aliases)
        if value in (None, "", [], ()):
            continue
        if target == "Name":
            result[target] = _title(value)
        elif target in {"PID", "EID", "Author ID", "Description"}:
            result[target] = _text(value)
        elif target in {"URL", "Audio URL"}:
            result[target] = {"url": _clean_url(value)}
        elif target in {
            "Total Listening Seconds",
            "Duration Seconds",
            "Played Seconds",
        }:
            result[target] = {"number": value}
        elif target == "Liked":
            result[target] = {"checkbox": bool(value)}
        elif target == "Listening Status":
            result[target] = {"select": {"name": str(value)}}
        elif target in {"Published At", "Last Played At", "Updated At"}:
            copied = _copy_date(value)
            if copied is not None:
                result[target] = copied
        elif target in {"Podcast", "Authors"} and isinstance(value, list):
            result[target] = {
                "relation": [dict(item) for item in value if isinstance(item, Mapping)]
            }
    return result


class LegacyTemplateMigrator:
    """Prepare and apply an additive migration in the original row pages."""

    def __init__(
        self,
        api: MigrationAPI,
        resources: Mapping[str, NotionResource],
        root_page_id: str,
    ) -> None:
        self.api = api
        self.resources = resources
        self.root_page_id = root_page_id

    def _rows(self, key: str) -> list[JsonObject]:
        resource = self.resources.get(key)
        if resource is None:
            return []
        return self.api.query_data_source(resource.data_source_id, {"page_size": 100})

    def _page_change(
        self,
        resource: str,
        page: Mapping[str, Any],
        mapping: Mapping[str, tuple[str, ...]],
        key_names: tuple[str, ...],
    ) -> PageMigration | None:
        properties = page.get("properties")
        if not isinstance(properties, Mapping) or not page.get("id"):
            return None
        stable_key = str(_value(properties, *key_names) or "")
        changes = _mapped(properties, mapping)
        if resource == "author" and not _nonempty(properties, "Author ID"):
            changes["Author ID"] = _text(f"legacy:{page['id']}")
            stable_key = stable_key or f"legacy:{page['id']}"
        if resource == "episode":
            status = _value(properties, "语音转文字状态")
            tongyi_url = _value(properties, "通义链接")
            if status == "Done" and not _nonempty(properties, "ASR Status"):
                changes.update(
                    {
                        "ASR Provider": _text("legacy"),
                        "ASR Task ID": _text(_legacy_task_id(tongyi_url)),
                        "ASR Status": {"select": {"name": "已发布"}},
                        "Content Version": _text("legacy-import-v1"),
                    }
                )
        if not changes:
            return None
        return PageMigration(resource, str(page["id"]), stable_key, changes)

    def _legacy_embeds(self, roots: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        visited: set[str] = set()

        def walk(block_id: str) -> None:
            if block_id in visited:
                return
            visited.add(block_id)
            for block in self.api.list_block_children(block_id):
                child_id = str(block.get("id") or "")
                block_type = block.get("type")
                body = block.get(block_type) if isinstance(block_type, str) else None
                url = body.get("url") if isinstance(body, Mapping) else None
                if (
                    child_id
                    and isinstance(url, str)
                    and urlparse(url).hostname in LEGACY_SERVICE_HOSTS
                ):
                    result.append(child_id)
                    continue
                if (
                    child_id
                    and block.get("has_children")
                    and block_type not in {"child_database", "child_page"}
                ):
                    walk(child_id)

        for root in roots:
            walk(root)
        return tuple(dict.fromkeys(result))

    def plan(self) -> MigrationPlan:
        changes: list[PageMigration] = []
        scanned = 0
        rows_by_resource = {
            "author": self._rows("author"),
            "podcast": self._rows("podcast"),
            "episode": self._rows("episode"),
        }
        specifications: dict[
            str,
            tuple[Mapping[str, tuple[str, ...]], tuple[str, ...]],
        ] = {
            "author": (
                {
                    "Name": ("标题",),
                },
                ("Author ID", "标题", "Name"),
            ),
            "podcast": (
                {
                    "Name": ("播客",),
                    "PID": ("Pid",),
                    "Description": ("Description",),
                    "URL": ("链接",),
                    "Total Listening Seconds": ("收听时长",),
                    "Updated At": ("最后更新时间",),
                    "Authors": ("作者",),
                },
                ("PID", "Pid"),
            ),
            "episode": (
                {
                    "Name": ("标题",),
                    "EID": ("Eid",),
                    "Description": ("Description",),
                    "Published At": ("发布时间", "时间戳"),
                    "Audio URL": ("音频",),
                    "Duration Seconds": ("时长",),
                    "Played Seconds": ("收听进度",),
                    "Listening Status": ("状态",),
                    "Liked": ("喜欢",),
                    "Last Played At": ("日期",),
                    "Podcast": ("Podcast",),
                },
                ("EID", "Eid"),
            ),
        }
        key_counts: Counter[str] = Counter()
        for resource, rows in rows_by_resource.items():
            mapping, key_names = specifications[resource]
            scanned += len(rows)
            for page in rows:
                properties = page.get("properties")
                if isinstance(properties, Mapping):
                    stable_key = str(_value(properties, *key_names) or "")
                    if stable_key and resource in {"podcast", "episode"}:
                        key_counts[f"{resource}:{stable_key}"] += 1
                change = self._page_change(resource, page, mapping, key_names)
                if change is not None:
                    changes.append(change)
        duplicate_keys = tuple(sorted(key for key, count in key_counts.items() if count > 1))
        episode_roots = tuple(
            str(page["id"]) for page in rows_by_resource["episode"] if page.get("id")
        )
        embeds = self._legacy_embeds((self.root_page_id, *episode_roots))
        return MigrationPlan(scanned, tuple(changes), embeds, duplicate_keys)

    def migrate(self, *, dry_run: bool) -> MigrationReport:
        plan = self.plan()
        if plan.duplicate_keys:
            return MigrationReport(
                plan.scanned_pages,
                len(plan.changes),
                0,
                len(plan.legacy_embed_ids),
                0,
                plan.duplicate_keys,
                dry_run,
            )
        if dry_run:
            return MigrationReport(
                plan.scanned_pages,
                len(plan.changes),
                0,
                len(plan.legacy_embed_ids),
                0,
                (),
                True,
            )
        for change in plan.changes:
            self.api.update_page(change.page_id, {"properties": change.properties})
        for block_id in plan.legacy_embed_ids:
            self.api.delete_block(block_id)
        return MigrationReport(
            plan.scanned_pages,
            len(plan.changes),
            len(plan.changes),
            len(plan.legacy_embed_ids),
            len(plan.legacy_embed_ids),
            (),
            False,
        )
