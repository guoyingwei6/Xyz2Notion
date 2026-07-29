"""Idempotent Notion workspace initializer for the clean-room template."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from xyz2notion.notion.client import JsonObject, NotionAPIError, rich_text
from xyz2notion.notion.schema import (
    DATABASE_SPECS,
    VIEW_SPECS,
    DatabaseSpec,
    NotionResource,
    ViewSpec,
    relational_properties,
    view_configuration,
)

DATA_PAGE_TITLE = "Xyz2Notion 数据层"
HOME_MARKER = "XYZ2NOTION_MANAGED_HOME_V1"
DEFAULT_COVER_URL = "https://raw.githubusercontent.com/guoyingwei6/Xyz2Notion/main/assets/cover.svg"
LEGACY_DATABASE_TITLES: dict[str, tuple[str, ...]] = {
    "author": ("Author", "作者"),
    "podcast": ("Podcast",),
    "episode": ("Episode",),
    "all": ("全部",),
    "year": ("年",),
    "month": ("月",),
    "week": ("周",),
    "day": ("日",),
    "mindmap": ("思维导图",),
}
LEGACY_SIGNATURES: dict[str, tuple[frozenset[str], ...]] = {
    "author": (frozenset({"Name"}), frozenset({"标题"})),
    "podcast": (
        frozenset({"Name", "PID"}),
        frozenset({"播客", "Pid"}),
    ),
    "episode": (
        frozenset({"Name", "EID"}),
        frozenset({"标题", "Eid", "音频"}),
    ),
    "all": (frozenset({"Name"}), frozenset({"标题"})),
    "year": (frozenset({"Name"}), frozenset({"标题"})),
    "month": (frozenset({"Name"}), frozenset({"标题"})),
    "week": (frozenset({"Name"}), frozenset({"标题"})),
    "day": (frozenset({"Name"}), frozenset({"标题"})),
    "mindmap": (frozenset({"Name"}), frozenset({"标题"})),
}


class NotionInitializerAPI(Protocol):
    """The API surface required by the initializer and its fakes."""

    def retrieve_page(self, page_id: str) -> JsonObject: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...

    def create_page(
        self,
        parent_page_id: str,
        title: str,
        *,
        icon: str | None = None,
    ) -> JsonObject: ...

    def search_databases(self, title: str) -> list[JsonObject]: ...

    def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: Mapping[str, Any],
        *,
        icon: str | None = None,
        is_inline: bool = False,
    ) -> JsonObject: ...

    def retrieve_database(self, database_id: str) -> JsonObject: ...

    def retrieve_data_source(self, data_source_id: str) -> JsonObject: ...

    def update_data_source(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
    ) -> JsonObject: ...

    def list_views(
        self,
        *,
        database_id: str | None = None,
        data_source_id: str | None = None,
    ) -> list[JsonObject]: ...

    def retrieve_view(self, view_id: str) -> JsonObject: ...

    def create_view(self, payload: Mapping[str, Any]) -> JsonObject: ...

    def update_view(self, view_id: str, payload: Mapping[str, Any]) -> JsonObject: ...

    def list_block_children(self, block_id: str) -> list[JsonObject]: ...

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]: ...


@dataclass(frozen=True)
class InitializationResult:
    """IDs and action counts produced by one initializer run."""

    data_page_id: str
    resources: dict[str, NotionResource]
    created_databases: int
    created_views: int
    updated_views: int
    created_home: bool


def _title_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("plain_text") is not None:
            parts.append(str(item["plain_text"]))
            continue
        text = item.get("text")
        if isinstance(text, dict) and text.get("content") is not None:
            parts.append(str(text["content"]))
    return "".join(parts)


def _database_title(database: Mapping[str, Any]) -> str:
    return _title_text(database.get("title"))


def _parent_page_id(resource: Mapping[str, Any]) -> str | None:
    parent = resource.get("parent")
    if not isinstance(parent, dict):
        return None
    page_id = parent.get("page_id")
    return str(page_id) if page_id else None


def _data_source_id(database: Mapping[str, Any]) -> str:
    sources = database.get("data_sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Notion database response has no data_sources")
    first = sources[0]
    if not isinstance(first, dict) or not first.get("id"):
        raise ValueError("Notion database response has an invalid data source")
    return str(first["id"])


def _property_ids(data_source: Mapping[str, Any]) -> dict[str, str]:
    properties = data_source.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        name: str(value["id"])
        for name, value in properties.items()
        if isinstance(value, dict) and value.get("id") is not None
    }


def _block_text(block: Mapping[str, Any]) -> str:
    block_type = block.get("type")
    body = block.get(str(block_type))
    if not isinstance(body, dict):
        return ""
    return _title_text(body.get("rich_text"))


def _heading(level: int, text: str) -> JsonObject:
    block_type = f"heading_{level}"
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich_text(text)},
    }


def _paragraph(text: str) -> JsonObject:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text(text)},
    }


def _callout(text: str, emoji: str) -> JsonObject:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": emoji},
            "rich_text": rich_text(text),
        },
    }


def home_blocks() -> list[JsonObject]:
    """Return original dashboard blocks; user blocks are never removed."""
    return [
        _paragraph(HOME_MARKER),
        _heading(1, "Xyz2Notion · 播客仪表盘"),
        _callout(
            "自主可控: 小宇宙、转写、总结和统计仅在你的 GitHub Actions 与 Notion 中流转。",
            "🔒",
        ),
        {
            "object": "block",
            "type": "column_list",
            "column_list": {
                "children": [
                    {
                        "object": "block",
                        "type": "column",
                        "column": {
                            "width_ratio": 0.5,
                            "children": [
                                _heading(2, "快速入口"),
                                _paragraph("Podcast · Episode · 思维导图"),
                            ],
                        },
                    },
                    {
                        "object": "block",
                        "type": "column",
                        "column": {
                            "width_ratio": 0.5,
                            "children": [
                                _heading(2, "同步状态"),
                                _paragraph("统计与内容由 Xyz2Notion 工作流幂等更新。"),
                            ],
                        },
                    },
                ]
            },
        },
        _heading(2, "年度收听热力图"),
        _callout("热力图将在 P5 统计同步后自动更新。", "🔥"),
        _heading(2, "收听统计"),
        _paragraph("以下视图由 Xyz2Notion 管理; 你添加的其他块、视图和笔记不会被删除。"),
    ]


class NotionInitializer:
    """Create or reconcile nine databases, relations, views, and dashboard blocks."""

    def __init__(self, api: NotionInitializerAPI, root_page_id: str) -> None:
        if not root_page_id.strip():
            raise ValueError("root_page_id cannot be empty")
        self.api = api
        self.root_page_id = root_page_id
        self._root_databases: dict[str, list[str]] | None = None

    def initialize(self) -> InitializationResult:
        """Run an additive, idempotent initialization."""
        self._ensure_page_branding()
        data_page_id = self._ensure_data_page()
        resources, created_databases = self._ensure_databases(data_page_id)
        resources = self._ensure_relations(resources)
        created_home = self._ensure_home_blocks()
        created_views, updated_views = self._ensure_views(resources)
        return InitializationResult(
            data_page_id=data_page_id,
            resources=resources,
            created_databases=created_databases,
            created_views=created_views,
            updated_views=updated_views,
            created_home=created_home,
        )

    def discover_existing_resources(self) -> dict[str, NotionResource]:
        """Discover adoptable databases without changing the Notion page."""
        resources: dict[str, NotionResource] = {}
        for spec in DATABASE_SPECS:
            database = self._find_database(spec, "__xyz2notion_discovery__")
            if database is None:
                continue
            data_source_id = _data_source_id(database)
            data_source = self.api.retrieve_data_source(data_source_id)
            resources[spec.key] = NotionResource(
                database_id=str(database["id"]),
                data_source_id=data_source_id,
                property_ids=_property_ids(data_source),
            )
        return resources

    def _ensure_page_branding(self) -> None:
        page = self.api.retrieve_page(self.root_page_id)
        updates: JsonObject = {}
        if not page.get("icon"):
            updates["icon"] = {"type": "emoji", "emoji": "🎧"}
        if not page.get("cover"):
            updates["cover"] = {
                "type": "external",
                "external": {"url": DEFAULT_COVER_URL},
            }
        if updates:
            self.api.update_page(self.root_page_id, updates)

    def _ensure_data_page(self) -> str:
        for block in self.api.list_block_children(self.root_page_id):
            if block.get("type") == "child_page":
                child = block.get("child_page")
                if isinstance(child, dict) and child.get("title") == DATA_PAGE_TITLE:
                    return str(block["id"])
        created = self.api.create_page(self.root_page_id, DATA_PAGE_TITLE, icon="📦")
        if not created.get("id"):
            raise ValueError("Notion create_page response has no id")
        return str(created["id"])

    def _find_database(self, spec: DatabaseSpec, parent_page_id: str) -> JsonObject | None:
        for candidate in self.api.search_databases(spec.title):
            if _database_title(candidate) != spec.title:
                continue
            database_id = candidate.get("id")
            if not database_id:
                continue
            database = self.api.retrieve_database(str(database_id))
            if _parent_page_id(database) == parent_page_id:
                return database
        for title in LEGACY_DATABASE_TITLES[spec.key]:
            for database_id in self._databases_under_root().get(title, []):
                database = self.api.retrieve_database(database_id)
                try:
                    data_source = self.api.retrieve_data_source(_data_source_id(database))
                except (KeyError, ValueError):
                    continue
                properties = data_source.get("properties")
                names = set(properties) if isinstance(properties, dict) else set()
                if any(signature.issubset(names) for signature in LEGACY_SIGNATURES[spec.key]):
                    return database
        return None

    def _databases_under_root(self) -> dict[str, list[str]]:
        """Find only database blocks inside the configured root page tree."""
        if self._root_databases is not None:
            return self._root_databases
        found: dict[str, list[str]] = {}
        visited: set[str] = set()

        def walk(block_id: str) -> None:
            if block_id in visited:
                return
            visited.add(block_id)
            for block in self.api.list_block_children(block_id):
                child_type = block.get("type")
                child_id = block.get("id")
                if child_type == "child_database" and child_id:
                    child = block.get("child_database")
                    if isinstance(child, dict) and child.get("title"):
                        found.setdefault(str(child["title"]), []).append(str(child_id))
                    continue
                if block.get("has_children") and child_id:
                    walk(str(child_id))

        walk(self.root_page_id)
        self._root_databases = found
        return found

    def _ensure_databases(
        self,
        parent_page_id: str,
    ) -> tuple[dict[str, NotionResource], int]:
        resources: dict[str, NotionResource] = {}
        created_count = 0
        for spec in DATABASE_SPECS:
            database = self._find_database(spec, parent_page_id)
            if database is None:
                database = self.api.create_database(
                    parent_page_id,
                    spec.title,
                    spec.properties,
                    icon=spec.icon,
                )
                created_count += 1
            database_id = str(database["id"])
            data_source_id = _data_source_id(database)
            self._add_missing_properties(data_source_id, spec.properties)
            data_source = self.api.retrieve_data_source(data_source_id)
            resources[spec.key] = NotionResource(
                database_id=database_id,
                data_source_id=data_source_id,
                property_ids=_property_ids(data_source),
            )
        return resources, created_count

    def _ensure_relations(
        self,
        resources: dict[str, NotionResource],
    ) -> dict[str, NotionResource]:
        for key, properties in relational_properties(resources).items():
            self._add_missing_properties(resources[key].data_source_id, properties)
        refreshed: dict[str, NotionResource] = {}
        for key, resource in resources.items():
            data_source = self.api.retrieve_data_source(resource.data_source_id)
            refreshed[key] = NotionResource(
                database_id=resource.database_id,
                data_source_id=resource.data_source_id,
                property_ids=_property_ids(data_source),
            )
        return refreshed

    def _add_missing_properties(
        self,
        data_source_id: str,
        desired: Mapping[str, Any],
    ) -> None:
        """Add schema fields without rewriting existing formulas, options, or relations."""
        data_source = self.api.retrieve_data_source(data_source_id)
        current = data_source.get("properties")
        current_names = set(current) if isinstance(current, dict) else set()
        missing = ((name, value) for name, value in desired.items() if name not in current_names)
        for name, value in missing:
            try:
                self.api.update_data_source(data_source_id, {name: value})
            except NotionAPIError as exc:
                raise NotionAPIError(
                    f"Failed to add Notion property {name!r}: {exc}",
                    status_code=exc.status_code,
                    code=exc.code,
                    retryable=exc.retryable,
                ) from exc

    def _ensure_home_blocks(self) -> bool:
        blocks = self.api.list_block_children(self.root_page_id)
        if any(HOME_MARKER in _block_text(block) for block in blocks):
            return False
        self.api.append_block_children(self.root_page_id, home_blocks())
        return True

    def _managed_view(
        self,
        spec: ViewSpec,
        source: NotionResource,
    ) -> JsonObject | None:
        for reference in self.api.list_views(data_source_id=source.data_source_id):
            view_id = reference.get("id")
            if not view_id:
                continue
            view = self.api.retrieve_view(str(view_id))
            if view.get("name") != spec.name:
                continue
            parent = view.get("parent")
            if not isinstance(parent, dict) or not parent.get("database_id"):
                continue
            parent_database_id = str(parent["database_id"])
            if parent_database_id == source.database_id:
                continue
            linked_database = self.api.retrieve_database(parent_database_id)
            if _parent_page_id(linked_database) == self.root_page_id:
                return view
        return None

    def _view_payload(
        self,
        spec: ViewSpec,
        source: NotionResource,
        *,
        creating: bool,
    ) -> JsonObject:
        payload: JsonObject = {
            "name": spec.name,
            "sorts": list(spec.sorts),
            "configuration": view_configuration(spec, source.property_ids),
        }
        if spec.filter is not None or not creating:
            payload["filter"] = spec.filter
        if creating:
            payload.update(
                {
                    "create_database": {
                        "parent": {
                            "type": "page_id",
                            "page_id": self.root_page_id,
                        }
                    },
                    "data_source_id": source.data_source_id,
                    "type": spec.view_type,
                }
            )
        return payload

    def _ensure_views(self, resources: dict[str, NotionResource]) -> tuple[int, int]:
        created = 0
        updated = 0
        for spec in VIEW_SPECS:
            source = resources[spec.source]
            existing = self._managed_view(spec, source)
            if existing is None:
                self.api.create_view(self._view_payload(spec, source, creating=True))
                created += 1
            else:
                self.api.update_view(
                    str(existing["id"]),
                    self._view_payload(spec, source, creating=False),
                )
                updated += 1
        return created, updated
