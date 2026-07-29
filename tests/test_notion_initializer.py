from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.initializer import (
    DATA_PAGE_TITLE,
    HOME_MARKER,
    HOME_MARKER_URL,
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
        return [
            {"id": view_id}
            for view_id, view in self.views.items()
            if (data_source_id is None or view["data_source_id"] == data_source_id)
            and (database_id is None or view["parent"]["database_id"] == database_id)
        ]

    def retrieve_view(self, view_id: str) -> JsonObject:
        return self.views[view_id]

    def create_view(self, payload: Mapping[str, Any]) -> JsonObject:
        self.created_views += 1
        view_id = f"view-{self.created_views}"
        linked_database_id = payload.get("database_id")
        if linked_database_id is None:
            linked_database_id = f"linked-{self.created_views}"
            create_database = payload["create_database"]
            self.databases[linked_database_id] = {
                "id": linked_database_id,
                "title": rich_text(str(payload["name"])),
                "parent": dict(create_database["parent"]),
                "data_sources": [{"id": payload["data_source_id"]}],
            }
            parent_page_id = str(create_database["parent"]["page_id"])
            self.blocks.setdefault(parent_page_id, []).append(
                {
                    "id": linked_database_id,
                    "type": "child_database",
                    "child_database": {"title": str(payload["name"])},
                }
            )
        else:
            linked_database_id = str(linked_database_id)
            assert self.databases[linked_database_id]["data_sources"] == [
                {"id": payload["data_source_id"]}
            ]
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

    def update_block(
        self,
        block_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        for blocks in self.blocks.values():
            for block in blocks:
                if block.get("id") == block_id:
                    block.update(payload)
                    return block
        raise KeyError(block_id)


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
    assert len(VIEW_SPECS) == 19
    assert {
        "收听总览",
        "年度趋势",
        "月度趋势",
        "周趋势",
        "每日趋势",
        "Podcast",
        "Episode · 全部",
        "Episode · 在听",
        "Episode · 听过",
        "Episode · 喜欢",
        "Episode · 待听",
        "Episode · 收藏",
        "思维导图",
    }.issubset({spec.name for spec in VIEW_SPECS})


def test_statistics_charts_are_compact_primary_views() -> None:
    chart_specs = [spec for spec in VIEW_SPECS if spec.view_type == "chart"]
    assert [spec.name for spec in chart_specs] == [
        "收听总览",
        "年度趋势",
        "月度趋势",
        "周趋势",
        "每日趋势",
    ]
    assert all(spec.position == "start" for spec in chart_specs)
    assert chart_specs[0].chart_type == "number"
    assert all(spec.chart_type in {"column", "line"} for spec in chart_specs[1:])
    assert [spec.chart_group_by for spec in chart_specs[1:]] == [
        "year",
        "month",
        "week",
        "day",
    ]
    assert all(
        spec.filter
        == {
            "property": "Exact Listening Seconds",
            "number": {"greater_than": 0},
        }
        for spec in chart_specs[1:]
    )


def test_initializer_creates_complete_clean_room_template() -> None:
    fake = FakeNotion()
    result = NotionInitializer(fake, "root").initialize()
    assert result.created_databases == 9
    assert result.created_views == len(VIEW_SPECS)
    assert result.updated_views == 0
    assert result.created_home is True
    assert fake.created_pages == 1
    assert fake.blocks["root"][1]["child_page"]["title"] == DATA_PAGE_TITLE
    assert fake.pages["root"]["icon"]["emoji"] == "🪐"
    assert fake.pages["root"]["cover"]["external"]["url"].endswith("assets/cover.svg?v=2")
    assert set(result.resources) == {spec.key for spec in DATABASE_SPECS}

    episode_properties = fake.data_sources[result.resources["episode"].data_source_id]["properties"]
    assert episode_properties["Podcast"]["relation"]["data_source_id"] == (
        result.resources["podcast"].data_source_id
    )
    assert episode_properties["Podcast"]["relation"]["dual_property"] == {
        "synced_property_name": "Episodes"
    }
    podcast_properties = fake.data_sources[result.resources["podcast"].data_source_id]["properties"]
    assert podcast_properties["Authors"]["relation"]["dual_property"] == {
        "synced_property_name": "Podcasts"
    }
    assert "Progress Percent" in episode_properties
    assert "Progress Ring" in episode_properties
    assert episode_properties["Progress Ring"]["rich_text"] == {}
    assert episode_properties["Playlist Position"]["number"] == {"format": "number"}
    assert "ASR Provider" in episode_properties
    assert "Content Version" in episode_properties

    year_properties = fake.data_sources[result.resources["year"].data_source_id]["properties"]
    assert year_properties["Listening Seconds"]["rollup"]["function"] == "sum"
    assert year_properties["Episode Count"]["rollup"]["function"] == "count"
    assert year_properties["Listening Hours"]["formula"]["expression"]

    liked_view = next(view for view in fake.views.values() if view["name"] == "Episode · 喜欢")
    assert liked_view["filter"] == {
        "and": [
            {"property": "Played Seconds", "number": {"greater_than": 0}},
            {"property": "Liked", "checkbox": {"equals": True}},
        ]
    }
    podcast_view = next(view for view in fake.views.values() if view["name"] == "Podcast")
    assert podcast_view["configuration"]["type"] == "gallery"
    assert podcast_view["configuration"]["cover"]["type"] == "property"
    assert podcast_view["filter"] == {
        "property": "Total Listening Seconds",
        "number": {"greater_than": 0},
    }

    linked_database_ids = {view["parent"]["database_id"] for view in fake.views.values()}
    assert len(linked_database_ids) == len({spec.source for spec in VIEW_SPECS})
    episode_views = [
        view
        for view in fake.views.values()
        if view["data_source_id"] == result.resources["episode"].data_source_id
    ]
    assert len(episode_views) == 6
    assert len({view["parent"]["database_id"] for view in episode_views}) == 1
    assert sum("create_database" in view for view in episode_views) == 1
    assert sum("database_id" in view for view in episode_views) == 5


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
    marker_count = sum(HOME_MARKER_URL in str(block) for block in fake.blocks["root"])
    assert marker_count == 1


def test_initializer_rebuilds_missing_view_in_existing_linked_database() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize()
    episode_data_source_id = first.resources["episode"].data_source_id
    missing_id = next(
        view_id for view_id, view in fake.views.items() if view["name"] == "Episode · 全部"
    )
    episode_database_id = str(fake.views[missing_id]["parent"]["database_id"])
    del fake.views[missing_id]
    child_databases_before = [
        block for block in fake.blocks["root"] if block["type"] == "child_database"
    ]

    rebuilt = initializer.initialize()

    assert rebuilt.created_views == 1
    assert rebuilt.updated_views == len(VIEW_SPECS) - 1
    replacement = next(view for view in fake.views.values() if view["name"] == "Episode · 全部")
    assert replacement["data_source_id"] == episode_data_source_id
    assert replacement["parent"]["database_id"] == episode_database_id
    assert replacement["database_id"] == episode_database_id
    assert "create_database" not in replacement
    assert [
        block for block in fake.blocks["root"] if block["type"] == "child_database"
    ] == child_databases_before


def test_initializer_migrates_legacy_home_columns_and_hides_marker() -> None:
    fake = FakeNotion()
    fake.blocks["root"].extend(
        [
            {
                "id": "legacy-marker",
                "type": "paragraph",
                "paragraph": {"rich_text": rich_text(HOME_MARKER)},
            },
            {
                "id": "columns",
                "type": "column_list",
                "has_children": True,
                "column_list": {},
            },
        ]
    )
    fake.blocks["columns"] = [
        {"id": "left", "type": "column", "has_children": True, "column": {}},
        {"id": "right", "type": "column", "has_children": True, "column": {}},
    ]
    fake.blocks["left"] = [
        {
            "id": "quick",
            "type": "heading_2",
            "heading_2": {"rich_text": rich_text("快速入口")},
        },
        {
            "id": "quick-copy",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text("Podcast · Episode · 思维导图")},
        },
    ]
    fake.blocks["right"] = [
        {
            "id": "status",
            "type": "heading_2",
            "heading_2": {"rich_text": rich_text("同步状态")},
        },
        {
            "id": "status-copy",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text("统计与内容由 Xyz2Notion 工作流幂等更新。")},
        },
    ]

    NotionInitializer(fake, "root").initialize()

    assert "菜单" in str(fake.blocks["left"])
    assert "待听" in str(fake.blocks["left"])
    assert "播客记录" in str(fake.blocks["right"])
    assert HOME_MARKER not in str(fake.blocks["root"])
    assert HOME_MARKER_URL in str(fake.blocks["root"])


def test_initializer_adopts_matching_legacy_database_without_copying_rows() -> None:
    fake = FakeNotion()
    legacy = fake.create_database(
        "root",
        "Podcast",
        {
            "播客": {"title": {}},
            "Pid": {"rich_text": {}},
            "用户笔记": {"rich_text": {}},
        },
    )
    fake.blocks["root"].append(
        {
            "id": legacy["id"],
            "type": "child_database",
            "child_database": {"title": "Podcast"},
        }
    )

    result = NotionInitializer(fake, "root").initialize()

    assert result.resources["podcast"].database_id == legacy["id"]
    assert result.created_databases == 8
    properties = fake.data_sources[result.resources["podcast"].data_source_id]["properties"]
    assert "PID" in properties
    assert "Name" in properties
    assert "用户笔记" in properties


def test_home_layout_has_columns_and_heatmap_placeholder() -> None:
    blocks = home_blocks()
    column_list = next(block for block in blocks if block["type"] == "column_list")
    assert len(column_list["column_list"]["children"]) == 2
    ratios = [column["column"]["width_ratio"] for column in column_list["column_list"]["children"]]
    assert ratios == [
        0.3,
        0.7,
    ]
    rendered = str(blocks)
    assert "播客记录" in rendered
    assert "只记录真正播放过的节目" in rendered
    assert "'type': 'table_of_contents'" in rendered
    assert "全部 · 在听 · 听过 · 喜欢 · 待听 · 收藏" in rendered
    assert rendered.index("Podcast") < rendered.index("Episode") < rendered.index("思维导图")


def test_initializer_preserves_existing_custom_icon_and_cover() -> None:
    fake = FakeNotion()
    fake.pages["root"]["icon"] = {"type": "emoji", "emoji": "🎧"}
    fake.pages["root"]["cover"] = {
        "type": "external",
        "external": {"url": "https://images.example/my-cover.jpg"},
    }

    NotionInitializer(fake, "root").initialize()

    assert fake.pages["root"]["icon"] == {"type": "emoji", "emoji": "🎧"}
    assert fake.pages["root"]["cover"]["external"]["url"] == ("https://images.example/my-cover.jpg")


def test_view_configuration_resolves_property_ids() -> None:
    spec = next(spec for spec in VIEW_SPECS if spec.key == "episodes_all")
    configuration = view_configuration(
        spec,
        {
            "Name": "title",
            "Listening Status": "status",
            "Progress Ring": "ring",
            "Played Seconds": "played",
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
    assert spec.filter == {
        "property": "Played Seconds",
        "number": {"greater_than": 0},
    }


def test_chart_configuration_uses_dates_hours_and_compact_presentation() -> None:
    month = next(spec for spec in VIEW_SPECS if spec.key == "months_chart")
    configuration = view_configuration(
        month,
        {
            "Start Date": "start",
            "Exact Listening Hours": "hours",
        },
    )
    assert configuration == {
        "type": "chart",
        "chart_type": "line",
        "x_axis": {
            "type": "date",
            "property_id": "start",
            "group_by": "month",
            "sort": {"type": "ascending"},
            "start_day_of_week": 1,
        },
        "y_axis": {
            "aggregator": "sum",
            "property_id": "hours",
        },
        "color_theme": "teal",
        "height": "small",
        "legend_position": "off",
        "show_data_labels": True,
        "axis_labels": "both",
        "grid_lines": "horizontal",
    }

    total = next(spec for spec in VIEW_SPECS if spec.key == "total_time_chart")
    assert view_configuration(total, {"Exact Listening Hours": "hours"}) == {
        "type": "chart",
        "chart_type": "number",
        "color_theme": "teal",
        "height": "small",
        "legend_position": "off",
        "show_data_labels": True,
        "value": {"aggregator": "sum", "property_id": "hours"},
    }


def test_chart_configuration_rejects_incomplete_chart_specs() -> None:
    month = next(spec for spec in VIEW_SPECS if spec.key == "months_chart")
    total = next(spec for spec in VIEW_SPECS if spec.key == "total_time_chart")

    with pytest.raises(ValueError, match="has no chart_type"):
        view_configuration(replace(month, chart_type=None), {})
    with pytest.raises(ValueError, match="has no value property"):
        view_configuration(total, {})
    with pytest.raises(ValueError, match="has incomplete axes"):
        view_configuration(month, {"Start Date": "start"})


def test_episode_views_have_user_facing_cards_and_expected_filters() -> None:
    episode_specs = {spec.key: spec for spec in VIEW_SPECS if spec.source == "episode"}
    assert set(episode_specs) == {
        "episodes_all",
        "episodes_listening",
        "episodes_played",
        "episodes_liked",
        "episodes_playlist",
        "episodes_favorited",
    }
    for spec in episode_specs.values():
        assert "Skip AI" not in spec.visible_properties
        assert "ASR Status" not in spec.visible_properties
        assert {"Name", "Listening Status", "Progress Ring"}.issubset(spec.visible_properties)

    playlist = episode_specs["episodes_playlist"]
    assert playlist.filter == {
        "property": "In Playlist",
        "checkbox": {"equals": True},
    }
    assert playlist.sorts == (
        {"property": "Playlist Position", "direction": "ascending"},
        {"property": "Published At", "direction": "descending"},
    )
    assert episode_specs["episodes_favorited"].filter == {
        "property": "Favorited",
        "checkbox": {"equals": True},
    }
