from collections.abc import Mapping, Sequence
from typing import Any

from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.initializer import (
    DATA_PAGE_TITLE,
    HOME_MARKER,
    NotionInitializer,
    home_blocks,
)
from xyz2notion.notion.schema import DATABASE_SPECS, VIEW_SPECS, view_configuration


class FakeNotion:
    def __init__(self) -> None:
        self.pages: dict[str, JsonObject] = {"root": {"id": "root", "icon": None, "cover": None}}
        self.blocks: dict[str, list[JsonObject]] = {
            "root": [
                {
                    "id": "user-block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text("用户自己的内容")},
                }
            ]
        }
        self.databases: dict[str, JsonObject] = {}
        self.data_sources: dict[str, JsonObject] = {}
        self.views: dict[str, JsonObject] = {}
        self.created_pages = 0
        self.created_databases = 0
        self.created_views = 0
        self.updated_views = 0

    def retrieve_page(self, page_id: str) -> JsonObject:
        return self.pages[page_id]

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.pages[page_id].update(payload)
        return self.pages[page_id]

    def create_page(
        self,
        parent_page_id: str,
        title: str,
        *,
        icon: str | None = None,
    ) -> JsonObject:
        self.created_pages += 1
        page_id = f"page-{self.created_pages}"
        page = {
            "id": page_id,
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "icon": {"type": "emoji", "emoji": icon} if icon else None,
        }
        self.pages[page_id] = page
        self.blocks[page_id] = []
        self.blocks[parent_page_id].append(
            {
                "id": page_id,
                "type": "child_page",
                "child_page": {"title": title},
            }
        )
        return page

    def search_databases(self, title: str) -> list[JsonObject]:
        return [
            database
            for database in self.databases.values()
            if "".join(item["text"]["content"] for item in database["title"]) == title
        ]

    def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: Mapping[str, Any],
        *,
        icon: str | None = None,
        is_inline: bool = False,
    ) -> JsonObject:
        del icon, is_inline
        self.created_databases += 1
        database_id = f"db-{self.created_databases}"
        data_source_id = f"ds-{self.created_databases}"
        database = {
            "id": database_id,
            "title": rich_text(title),
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "data_sources": [{"id": data_source_id, "name": title}],
        }
        self.databases[database_id] = database
        self.data_sources[data_source_id] = {"id": data_source_id, "properties": {}}
        self.update_data_source(data_source_id, properties)
        return database

    def retrieve_database(self, database_id: str) -> JsonObject:
        return self.databases[database_id]

    def retrieve_data_source(self, data_source_id: str) -> JsonObject:
        return self.data_sources[data_source_id]

    def update_data_source(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
    ) -> JsonObject:
        current = self.data_sources[data_source_id]["properties"]
        for name, value in properties.items():
            current[name] = {"id": f"{data_source_id}:{name}", **dict(value)}
        return self.data_sources[data_source_id]

    def list_views(
        self,
        *,
        database_id: str | None = None,
        data_source_id: str | None = None,
    ) -> list[JsonObject]:
        del database_id
        return [
            {"id": view_id}
            for view_id, view in self.views.items()
            if data_source_id is None or view["data_source_id"] == data_source_id
        ]

    def retrieve_view(self, view_id: str) -> JsonObject:
        return self.views[view_id]

    def create_view(self, payload: Mapping[str, Any]) -> JsonObject:
        self.created_views += 1
        view_id = f"view-{self.created_views}"
        linked_database_id = f"linked-{self.created_views}"
        create_database = payload["create_database"]
        self.databases[linked_database_id] = {
            "id": linked_database_id,
            "title": rich_text(str(payload["name"])),
            "parent": dict(create_database["parent"]),
            "data_sources": [{"id": payload["data_source_id"]}],
        }
        view = {
            "id": view_id,
            "parent": {"type": "database_id", "database_id": linked_database_id},
            **dict(payload),
        }
        self.views[view_id] = view
        return view

    def update_view(self, view_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.updated_views += 1
        self.views[view_id].update(payload)
        return self.views[view_id]

    def list_block_children(self, block_id: str) -> list[JsonObject]:
        return list(self.blocks[block_id])

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]:
        created = [dict(child) for child in children]
        for index, child in enumerate(created, start=len(self.blocks[block_id]) + 1):
            child.setdefault("id", f"block-{index}")
        self.blocks[block_id].extend(created)
        return created


def test_schema_has_exactly_nine_databases_and_expected_views() -> None:
    assert [spec.title for spec in DATABASE_SPECS] == [
        "Author",
        "Podcast",
        "Episode",
        "全部",
        "年",
        "月",
        "周",
        "日",
        "思维导图",
    ]
    assert len(VIEW_SPECS) == 12
    assert {
        "Podcast",
        "Episode · 全部",
        "Episode · 在听",
        "Episode · 听过",
        "Episode · 喜欢",
        "思维导图",
    }.issubset({spec.name for spec in VIEW_SPECS})


def test_initializer_creates_complete_clean_room_template() -> None:
    fake = FakeNotion()
    result = NotionInitializer(fake, "root").initialize()
    assert result.created_databases == 9
    assert result.created_views == len(VIEW_SPECS)
    assert result.updated_views == 0
    assert result.created_home is True
    assert fake.created_pages == 1
    assert fake.blocks["root"][1]["child_page"]["title"] == DATA_PAGE_TITLE
    assert fake.pages["root"]["icon"]["emoji"] == "🎧"
    assert fake.pages["root"]["cover"]["external"]["url"].endswith("assets/cover.svg")
    assert set(result.resources) == {spec.key for spec in DATABASE_SPECS}

    episode_properties = fake.data_sources[result.resources["episode"].data_source_id]["properties"]
    assert episode_properties["Podcast"]["relation"]["data_source_id"] == (
        result.resources["podcast"].data_source_id
    )
    assert "Progress Percent" in episode_properties
    assert "Progress Ring" in episode_properties
    assert "ASR Provider" in episode_properties
    assert "Content Version" in episode_properties

    year_properties = fake.data_sources[result.resources["year"].data_source_id]["properties"]
    assert year_properties["Listening Seconds"]["rollup"]["function"] == "sum"
    assert year_properties["Listening Hours"]["formula"]["expression"]

    liked_view = next(view for view in fake.views.values() if view["name"] == "Episode · 喜欢")
    assert liked_view["filter"] == {
        "property": "Liked",
        "checkbox": {"equals": True},
    }
    podcast_view = next(view for view in fake.views.values() if view["name"] == "Podcast")
    assert podcast_view["configuration"]["type"] == "gallery"
    assert podcast_view["configuration"]["cover"]["type"] == "property"


def test_initializer_is_idempotent_and_preserves_user_content() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize()
    user_block = {
        "id": "user-second",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text("我的笔记")},
    }
    fake.blocks["root"].append(user_block)
    podcast_ds = first.resources["podcast"].data_source_id
    fake.data_sources[podcast_ds]["properties"]["My Notes"] = {
        "id": "custom",
        "rich_text": {},
    }

    second = initializer.initialize()
    assert second.created_databases == 0
    assert second.created_views == 0
    assert second.updated_views == len(VIEW_SPECS)
    assert second.created_home is False
    assert fake.created_pages == 1
    assert fake.created_databases == 9
    assert fake.created_views == len(VIEW_SPECS)
    assert fake.data_sources[podcast_ds]["properties"]["My Notes"]["id"] == "custom"
    assert user_block in fake.blocks["root"]
    marker_count = sum(
        HOME_MARKER
        in "".join(
            item["text"]["content"]
            for item in block.get(block.get("type"), {}).get("rich_text", [])
        )
        for block in fake.blocks["root"]
    )
    assert marker_count == 1


def test_home_layout_has_columns_and_heatmap_placeholder() -> None:
    blocks = home_blocks()
    column_list = next(block for block in blocks if block["type"] == "column_list")
    assert len(column_list["column_list"]["children"]) == 2
    rendered = str(blocks)
    assert "年度收听热力图" in rendered
    assert "P5" in rendered


def test_view_configuration_resolves_property_ids() -> None:
    spec = next(spec for spec in VIEW_SPECS if spec.key == "episodes_all")
    configuration = view_configuration(
        spec,
        {
            "Name": "title",
            "Listening Status": "status",
            "Progress Ring": "ring",
            "Published At": "published",
            "Cover": "cover",
        },
    )
    assert configuration["cover"] == {"type": "property", "property_id": "cover"}
    assert {item["property_id"] for item in configuration["properties"]} == {
        "title",
        "status",
        "ring",
        "published",
    }
