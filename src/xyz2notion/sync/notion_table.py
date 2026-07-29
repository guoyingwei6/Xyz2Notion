"""Non-destructive row-level upserts for one Notion data source."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from xyz2notion.notion.client import JsonObject


class NotionRowsAPI(Protocol):
    """Notion methods used by metadata row synchronization."""

    def query_data_source(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]: ...

    def create_data_source_page(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
        *,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
        children: Sequence[Mapping[str, Any]] = (),
    ) -> JsonObject: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...


@dataclass(frozen=True)
class UpsertResult:
    """Outcome for one stable-key row."""

    page_id: str
    action: str
    changed_properties: tuple[str, ...] = ()


def _plain_text(items: object) -> str:
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("plain_text") is not None:
            parts.append(str(item["plain_text"]))
            continue
        text = item.get("text")
        if isinstance(text, Mapping) and text.get("content") is not None:
            parts.append(str(text["content"]))
    return "".join(parts)


def _canonical_property(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    if "title" in value:
        return ("title", _plain_text(value["title"]))
    if "rich_text" in value:
        return ("rich_text", _plain_text(value["rich_text"]))
    if "number" in value:
        return ("number", value["number"])
    if "checkbox" in value:
        return ("checkbox", bool(value["checkbox"]))
    if "url" in value:
        return ("url", value["url"])
    if "select" in value:
        selected = value["select"]
        return (
            "select",
            selected.get("name") if isinstance(selected, Mapping) else None,
        )
    if "date" in value:
        selected = value["date"]
        if not isinstance(selected, Mapping):
            return ("date", None)
        return ("date", selected.get("start"), selected.get("end"))
    if "relation" in value:
        relations = value["relation"]
        if not isinstance(relations, list):
            return ("relation", ())
        return (
            "relation",
            tuple(
                sorted(
                    str(item["id"])
                    for item in relations
                    if isinstance(item, Mapping) and item.get("id")
                )
            ),
        )
    if "files" in value:
        files = value["files"]
        if not isinstance(files, list):
            return ("files", ())
        urls: list[str] = []
        for item in files:
            if not isinstance(item, Mapping):
                continue
            external = item.get("external")
            file_value = item.get("file")
            if isinstance(external, Mapping) and external.get("url"):
                urls.append(str(external["url"]))
            elif isinstance(file_value, Mapping) and file_value.get("url"):
                urls.append(str(file_value["url"]))
        return ("files", tuple(urls))
    return tuple(sorted((str(key), repr(item)) for key, item in value.items()))


def _external_url(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    external = value.get("external")
    if isinstance(external, Mapping) and external.get("url"):
        return str(external["url"])
    return None


class NotionTable:
    """Cached stable-key index that only writes changed managed fields."""

    def __init__(
        self,
        api: NotionRowsAPI,
        data_source_id: str,
        key_property: str,
    ) -> None:
        self.api = api
        self.data_source_id = data_source_id
        self.key_property = key_property
        self._pages = self._load_pages()

    def _load_pages(self) -> dict[str, JsonObject]:
        result: dict[str, JsonObject] = {}
        for page in self.api.query_data_source(
            self.data_source_id,
            {"page_size": 100},
        ):
            properties = page.get("properties")
            if not isinstance(properties, Mapping):
                continue
            key = _plain_text(_mapping_property(properties.get(self.key_property)))
            if not key:
                continue
            if key in result:
                raise ValueError(
                    f"Duplicate Notion key {self.key_property}={key!r} "
                    f"in data source {self.data_source_id}"
                )
            result[key] = page
        return result

    def upsert(
        self,
        key: str,
        properties: Mapping[str, Any],
        *,
        create_only_properties: Mapping[str, Any] | None = None,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
    ) -> UpsertResult:
        """Create or minimally update a row without touching unknown fields."""
        existing = self._pages.get(key)
        if existing is None:
            create_properties = dict(properties)
            create_properties.update(create_only_properties or {})
            page = self.api.create_data_source_page(
                self.data_source_id,
                create_properties,
                icon=icon,
                cover=cover,
            )
            page_id = str(page["id"])
            self._pages[key] = {
                **page,
                "id": page_id,
                "properties": create_properties,
                "icon": icon,
                "cover": cover,
            }
            return UpsertResult(
                page_id=page_id,
                action="created",
                changed_properties=tuple(create_properties),
            )

        page_id = str(existing["id"])
        existing_properties = existing.get("properties")
        current = existing_properties if isinstance(existing_properties, Mapping) else {}
        changed = {
            name: value
            for name, value in properties.items()
            if _canonical_property(current.get(name)) != _canonical_property(value)
        }
        payload: JsonObject = {}
        if changed:
            payload["properties"] = changed
        if icon is not None and _external_url(existing.get("icon")) != _external_url(icon):
            payload["icon"] = dict(icon)
        if cover is not None and _external_url(existing.get("cover")) != _external_url(cover):
            payload["cover"] = dict(cover)
        if not payload:
            return UpsertResult(page_id=page_id, action="unchanged")

        updated = self.api.update_page(page_id, payload)
        merged_properties = dict(current)
        merged_properties.update(changed)
        self._pages[key] = {
            **existing,
            **updated,
            "properties": merged_properties,
            "icon": icon if "icon" in payload else existing.get("icon"),
            "cover": cover if "cover" in payload else existing.get("cover"),
        }
        return UpsertResult(
            page_id=page_id,
            action="updated",
            changed_properties=tuple(changed),
        )


def _mapping_property(value: object) -> object:
    if not isinstance(value, Mapping):
        return []
    if "title" in value:
        return value["title"]
    if "rich_text" in value:
        return value["rich_text"]
    return []
