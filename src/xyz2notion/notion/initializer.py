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
HOME_MARKER_URL = "https://xyz2notion.local/managed-home-v2"
DEFAULT_COVER_URL = "https://raw.githubusercontent.com/guoyingwei6/Xyz2Notion/main/assets/cover.svg"
MANAGED_COVER_PATH = "guoyingwei6/Xyz2Notion/main/assets/cover.svg"
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

    def update_block(
        self,
        block_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject: ...


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


def _home_marker() -> JsonObject:
    """Return an invisible linked marker that does not clutter the dashboard."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": "\u200b",
                        "link": {"url": HOME_MARKER_URL},
                    },
                }
            ]
        },
    }


def _is_home_marker(block: Mapping[str, Any]) -> bool:
    if HOME_MARKER in _block_text(block):
        return True
    body = block.get(str(block.get("type")))
    if not isinstance(body, Mapping):
        return False
    items = body.get("rich_text")
    if not isinstance(items, Sequence):
        return False
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("href") == HOME_MARKER_URL:
            return True
        text = item.get("text")
        link = text.get("link") if isinstance(text, Mapping) else None
        if isinstance(link, Mapping) and link.get("url") == HOME_MARKER_URL:
            return True
    return False


_MANAGED_HOME_SHAPE = (
    "heading_1",
    "callout",
    "column_list",
    "divider",
    "callout",
    "paragraph",
)


def _has_managed_home_layout(blocks: Sequence[Mapping[str, Any]]) -> bool:
    """Recognize the managed layout even when Notion reshapes its root blocks."""
    block_types = [str(block.get("type") or "") for block in blocks]
    width = len(_MANAGED_HOME_SHAPE)
    if any(
        tuple(block_types[index : index + width]) == _MANAGED_HOME_SHAPE
        for index in range(len(block_types) - width + 1)
    ):
        return True

    # Notion may omit the zero-width marker paragraph and place linked databases
    # between the remaining layout blocks.  The managed title and column layout
    # are stable, visible anchors that survive both transformations.
    managed_titles = {"播客", "Xyz2Notion · 播客仪表盘"}
    has_managed_title = any(
        block.get("type") == "heading_1" and _block_text(block) in managed_titles
        for block in blocks
    )
    return has_managed_title and "column_list" in block_types


def _divider() -> JsonObject:
    return {
        "object": "block",
        "type": "divider",
        "divider": {},
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


def _menu() -> JsonObject:
    return {
        "object": "block",
        "type": "table_of_contents",
        "table_of_contents": {"color": "gray"},
    }


def home_blocks() -> list[JsonObject]:
    """Return the Notion-native dashboard introduction matching the public demo."""
    return [
        _heading(1, "播客"),
        _callout(
            "这里只记录真正播放过的节目; 浏览但没有产生播放进度的单集不会参与统计。",
            "🎧",
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
                            "width_ratio": 0.3,
                            "children": [
                                _heading(2, "菜单"),
                                _menu(),
                                _divider(),
                                _heading(2, "总收听时长"),
                                _callout(
                                    "全部 · 年 · 月 · 周 · 日\n"
                                    "统计只计算实际播放时长大于 0 的节目。",
                                    "🎧",
                                ),
                                _heading(2, "收听时长排行"),
                                _paragraph("按真实播放时长从高到低展示 Podcast。"),
                            ],
                        },
                    },
                    {
                        "object": "block",
                        "type": "column",
                        "column": {
                            "width_ratio": 0.7,
                            "children": [
                                _heading(2, "播客记录"),
                                _callout(
                                    "年度收听热力图由每日同步工作流自动更新; "
                                    "未播放的单集不会点亮记录。",
                                    "📅",
                                ),
                                _heading(2, "Podcast"),
                                _paragraph("封面画廊 · 仅展示实际收听过的播客"),
                                _heading(2, "Episode"),
                                _paragraph("全部 · 在听 · 听过 · 喜欢 · 待听 · 收藏"),
                                _heading(2, "思维导图"),
                                _paragraph("每期节目生成的可视化脑图与原生大纲"),
                            ],
                        },
                    },
                ]
            },
        },
        _divider(),
        _callout(
            "下方数据库视图由 Xyz2Notion 更新; 你自己的笔记、字段、封面和图标会被保留。",
            "🪐",
        ),
        _home_marker(),
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
            updates["icon"] = {"type": "emoji", "emoji": "🪐"}
        cover = page.get("cover")
        external = cover.get("external") if isinstance(cover, dict) else None
        cover_url = external.get("url") if isinstance(external, dict) else None
        if not cover or (isinstance(cover_url, str) and MANAGED_COVER_PATH in cover_url):
            updates["cover"] = {
                "type": "external",
                "external": {"url": f"{DEFAULT_COVER_URL}?v=2"},
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
        marker = next((block for block in blocks if _is_home_marker(block)), None)
        if marker is None and _has_managed_home_layout(blocks):
            return False
        if marker is not None:
            replacements = {
                "Xyz2Notion · 播客仪表盘": ("heading_1", "播客"),
                ("自主可控: 小宇宙、转写、总结和统计仅在你的 GitHub Actions 与 Notion 中流转。"): (
                    "callout",
                    "这里只记录真正播放过的节目; 浏览但没有产生播放进度的单集不会参与统计。",
                ),
                "年度收听热力图": ("heading_2", "播客记录"),
                "热力图将在 P5 统计同步后自动更新。": (
                    "callout",
                    "年度收听热力图由每日同步工作流自动更新。",
                ),
                "收听统计": ("heading_2", "总收听时长"),
                ("以下视图由 Xyz2Notion 管理; 你添加的其他块、视图和笔记不会被删除。"): (
                    "paragraph",
                    "年、月、周、日统计只计算实际播放秒数大于 0 的节目。",
                ),
                "快速入口": ("heading_2", "菜单"),
                "Podcast · Episode · 思维导图": (
                    "paragraph",
                    "总收听时长\n收听时长排行\nAuthor\nPodcast\nEpisode\n待听\n收藏\n思维导图",
                ),
                "同步状态": ("heading_2", "播客记录"),
                "统计与内容由 Xyz2Notion 工作流幂等更新。": (
                    "paragraph",
                    "年度收听热力图由每日同步工作流自动更新; 未播放的单集不会点亮记录。",
                ),
            }
            layout_blocks = list(blocks)
            for block in blocks:
                if (
                    block.get("type") != "column_list"
                    or not block.get("has_children")
                    or not block.get("id")
                ):
                    continue
                columns = self.api.list_block_children(str(block["id"]))
                layout_blocks.extend(columns)
                for column in columns:
                    if column.get("type") == "column" and column.get("id"):
                        layout_blocks.extend(self.api.list_block_children(str(column["id"])))
            for block in layout_blocks:
                block_id = block.get("id")
                replacement = replacements.get(_block_text(block))
                if not block_id or replacement is None:
                    continue
                block_type, text = replacement
                body: JsonObject = {"rich_text": rich_text(text)}
                if block_type == "callout":
                    body["icon"] = {"type": "emoji", "emoji": "🎧"}
                self.api.update_block(str(block_id), {block_type: body})
            marker_id = marker.get("id")
            if marker_id and HOME_MARKER in _block_text(marker):
                marker_body = _home_marker()["paragraph"]
                self.api.update_block(str(marker_id), {"paragraph": marker_body})
            return False
        self.api.append_block_children(self.root_page_id, home_blocks())
        return True

    def _managed_views(self) -> dict[tuple[str, str], JsonObject]:
        """Index the first visible linked view for each data source and name."""
        result: dict[tuple[str, str], JsonObject] = {}
        for block in self.api.list_block_children(self.root_page_id):
            if block.get("type") != "child_database" or not block.get("id"):
                continue
            database_id = str(block["id"])
            for reference in self.api.list_views(database_id=database_id):
                view_id = reference.get("id")
                if not view_id:
                    continue
                view = self.api.retrieve_view(str(view_id))
                data_source_id = view.get("data_source_id")
                name = view.get("name")
                if not data_source_id or not name:
                    continue
                result.setdefault((str(data_source_id), str(name)), view)
        return result

    def _view_payload(
        self,
        spec: ViewSpec,
        source: NotionResource,
        *,
        creating: bool,
        database_id: str | None = None,
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
                    "data_source_id": source.data_source_id,
                    "type": spec.view_type,
                }
            )
            if database_id is None:
                payload["create_database"] = {
                    "parent": {
                        "type": "page_id",
                        "page_id": self.root_page_id,
                    }
                }
            else:
                payload["database_id"] = database_id
                payload["position"] = {"type": spec.position}
        return payload

    def _ensure_views(self, resources: dict[str, NotionResource]) -> tuple[int, int]:
        created = 0
        updated = 0
        managed_views = self._managed_views()
        linked_databases: dict[str, str] = {}
        for (data_source_id, _name), view in managed_views.items():
            parent = view.get("parent")
            if isinstance(parent, dict) and parent.get("database_id"):
                linked_databases.setdefault(data_source_id, str(parent["database_id"]))
        for spec in VIEW_SPECS:
            source = resources[spec.source]
            existing = managed_views.get((source.data_source_id, spec.name))
            if existing is None:
                view = self.api.create_view(
                    self._view_payload(
                        spec,
                        source,
                        creating=True,
                        database_id=linked_databases.get(source.data_source_id),
                    )
                )
                parent = view.get("parent")
                if not isinstance(parent, dict) or not parent.get("database_id"):
                    raise ValueError("Notion create_view response has no parent database ID")
                linked_databases.setdefault(source.data_source_id, str(parent["database_id"]))
                managed_views[(source.data_source_id, spec.name)] = view
                created += 1
            else:
                self.api.update_view(
                    str(existing["id"]),
                    self._view_payload(spec, source, creating=False),
                )
                updated += 1
        return created, updated
