from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from xyz2notion.notion.client import JsonObject, rich_text
from xyz2notion.notion.initializer import (
    DATA_PAGE_TITLE,
    HOME_MARKER,
    HOME_MARKER_URL,
    HOME_SUMMARY_MARKER_URL,
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
        self.deleted_view_ids: list[str] = []

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
            linked_sources = self.databases[linked_database_id]["data_sources"]
            if {"id": payload["data_source_id"]} not in linked_sources:
                linked_sources.append({"id": payload["data_source_id"]})
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

    def delete_view(self, view_id: str) -> JsonObject:
        self.deleted_view_ids.append(view_id)
        return self.views.pop(view_id)

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
        "Episode · 在听",
        "Episode · 听过",
        "Episode · 喜欢",
        "Episode · 待听",
        "Episode · 收藏",
        "转写文本",
        "AI总结与思维导图",
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
    assert chart_specs[1].filter is None
    for spec, window in zip(
        chart_specs[2:],
        ("past_year", "past_month", "past_week"),
        strict=True,
    ):
        assert spec.filter == {"property": "Start Date", "date": {window: {}}}


def test_statistics_tables_keep_complete_history() -> None:
    table_specs = {
        spec.name: spec
        for spec in VIEW_SPECS
        if spec.name in {"年度统计", "月度统计", "周统计", "日统计"}
    }
    assert set(table_specs) == {"年度统计", "月度统计", "周统计", "日统计"}
    assert all(spec.filter is None for spec in table_specs.values())


def test_initializer_creates_complete_clean_room_template() -> None:
    fake = FakeNotion()
    fake.blocks["root"] = []
    result = NotionInitializer(fake, "root").initialize(create_home=True)
    assert result.created_databases == 9
    assert result.created_views == len(VIEW_SPECS) - 1
    assert result.updated_views == 0
    assert result.created_home is True
    assert fake.created_pages == 1
    data_page_block = next(block for block in fake.blocks["root"] if block["type"] == "child_page")
    assert data_page_block["child_page"]["title"] == DATA_PAGE_TITLE
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
    assert (
        podcast_properties["收听小时"]["formula"]["expression"]
        == 'round(prop("Total Listening Seconds") / 3600 * 10) / 10'
    )
    assert "Progress Percent" in episode_properties
    assert "Progress Ring" in episode_properties
    assert episode_properties["Progress Ring"]["rich_text"] == {}
    assert episode_properties["Playlist Position"]["number"] == {"format": "number"}
    assert "ASR Provider" in episode_properties
    assert "增强 Provider" in episode_properties
    assert "增强状态" in episode_properties
    assert "Content Version" in episode_properties
    assert "转写完成时间" in episode_properties
    assert "总结完成时间" in episode_properties

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
    assert {item["property_id"] for item in podcast_view["configuration"]["properties"]} == {
        podcast_properties["Name"]["id"],
        podcast_properties["收听小时"]["id"],
    }

    ranking_view = next(view for view in fake.views.values() if view["name"] == "收听时长排行榜")
    ranking_property_ids = {
        item["property_id"] for item in ranking_view["configuration"]["properties"]
    }
    assert podcast_properties["收听小时"]["id"] in ranking_property_ids
    assert podcast_properties["Total Listening Seconds"]["id"] not in ranking_property_ids
    assert "Statistics Baseline Seconds" in podcast_properties
    assert "Statistics Baseline Seconds" in episode_properties
    assert "Statistics Baseline Date" in episode_properties
    assert "Statistics Ledger" in episode_properties
    assert "Statistics Baseline Version" in year_properties

    linked_database_ids = {view["parent"]["database_id"] for view in fake.views.values()}
    assert len(linked_database_ids) == 4
    statistics_views = [
        view
        for view in fake.views.values()
        if view["data_source_id"]
        in {
            result.resources[source].data_source_id
            for source in ("all", "year", "month", "week", "day")
        }
    ]
    assert len({view["parent"]["database_id"] for view in statistics_views}) == 1
    assert "收听总览" not in {view["name"] for view in statistics_views}
    statistics_database = statistics_views[0]["parent"]["database_id"]
    create_position = next(
        view["create_database"]["position"]
        for view in statistics_views
        if "create_database" in view
    )
    overview_anchor = next(
        block["id"]
        for block in fake.blocks["root"]
        if block["type"] == "paragraph" and HOME_SUMMARY_MARKER_URL in str(block)
    )
    assert create_position == {
        "type": "after_block",
        "block_id": overview_anchor,
    }
    assert fake.databases[statistics_database]["data_sources"] == [
        {"id": result.resources[source].data_source_id}
        for source in ("all", "year", "month", "week", "day")
    ]
    episode_views = [
        view
        for view in fake.views.values()
        if view["data_source_id"] == result.resources["episode"].data_source_id
    ]
    assert len(episode_views) == 7
    native_episode_views = [view for view in episode_views if view["name"].startswith("Episode · ")]
    assert len(native_episode_views) == 5
    assert len({view["parent"]["database_id"] for view in native_episode_views}) == 1
    ai_transcript_view = next(view for view in episode_views if view["name"] == "转写文本")
    mindmap_view = next(view for view in fake.views.values() if view["name"] == "AI总结与思维导图")
    assert ai_transcript_view["parent"]["database_id"] == mindmap_view["parent"]["database_id"]
    assert mindmap_view["data_source_id"] == result.resources["episode"].data_source_id
    assert mindmap_view["filter"] == {
        "or": [
            {"property": "增强状态", "select": {"equals": "已完成"}},
            {"property": "增强状态", "select": {"equals": "可重试失败"}},
            {"property": "增强状态", "select": {"equals": "最终失败"}},
            {"property": "ASR Status", "select": {"equals": "已增强"}},
            {"property": "ASR Status", "select": {"equals": "已发布"}},
            {"property": "ASR Status", "select": {"equals": "可重试失败"}},
            {"property": "ASR Status", "select": {"equals": "最终失败"}},
        ]
    }
    assert sum("create_database" in view for view in native_episode_views) == 1
    assert sum("database_id" in view for view in native_episode_views) == 4
    assert ai_transcript_view["sorts"] == [{"property": "转写完成时间", "direction": "descending"}]
    assert ai_transcript_view["filter"] == {
        "or": [
            {"property": "ASR Status", "select": {"equals": "已转写"}},
            {"property": "ASR Status", "select": {"equals": "已增强"}},
            {"property": "ASR Status", "select": {"equals": "已发布"}},
            {"property": "ASR Status", "select": {"equals": "可重试失败"}},
            {"property": "ASR Status", "select": {"equals": "最终失败"}},
        ]
    }


def test_initializer_is_idempotent_and_preserves_user_content() -> None:
    fake = FakeNotion()
    fake.blocks["root"] = []
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize(create_home=True)
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
    assert second.updated_views == len(VIEW_SPECS) - 1
    assert second.deleted_views == 0
    assert second.created_home is False
    assert fake.created_pages == 1
    assert fake.created_databases == 9
    assert fake.created_views == len(VIEW_SPECS) - 1
    assert fake.data_sources[podcast_ds]["properties"]["My Notes"]["id"] == "custom"
    assert user_block in fake.blocks["root"]
    marker_count = sum(HOME_MARKER_URL in str(block) for block in fake.blocks["root"])
    assert marker_count == 1


def test_initializer_sanitizes_stale_ai_view_configurations_and_keeps_valid_additions() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize(create_home=True)

    transcript_view = next(view for view in fake.views.values() if view["name"] == "转写文本")
    mindmap_view = next(view for view in fake.views.values() if view["name"] == "AI总结与思维导图")
    episode_property_ids = first.resources["episode"].property_ids
    oversized_configuration = {
        "type": "table",
        "properties": [
            {"property_id": f"historical-{index}", "visible": index % 2 == 0} for index in range(98)
        ],
        "wrap_cells": False,
        "frozen_column_index": 1,
        "show_vertical_lines": True,
    }
    oversized_configuration["properties"].extend(
        [
            {"property_id": episode_property_ids["Name"], "visible": True},
            {"property_id": episode_property_ids["Cover"], "visible": False},
        ]
    )
    transcript_view["configuration"] = dict(oversized_configuration)
    mindmap_view["configuration"] = dict(oversized_configuration)

    result = initializer.initialize()

    assert result.updated_views == len(VIEW_SPECS) - 1
    transcript_spec = next(spec for spec in VIEW_SPECS if spec.key == "episodes_transcript")
    mindmap_spec = next(spec for spec in VIEW_SPECS if spec.key == "mindmaps")
    for view, spec, expected_count in (
        (transcript_view, transcript_spec, 6),
        (mindmap_view, mindmap_spec, 8),
    ):
        property_ids = {item["property_id"] for item in view["configuration"]["properties"]}
        desired_ids = {
            item["property_id"]
            for item in view_configuration(spec, episode_property_ids)["properties"]
        }
        assert desired_ids.issubset(property_ids)
        assert episode_property_ids["Cover"] in property_ids
        assert len(property_ids) == expected_count
        assert all(not property_id.startswith("historical-") for property_id in property_ids)
    assert transcript_view["configuration"]["properties"][1] == {
        "property_id": episode_property_ids["Cover"],
        "visible": False,
    }
    assert transcript_view["configuration"]["wrap_cells"] is False
    assert mindmap_view["configuration"]["wrap_cells"] is False
    assert transcript_view["sorts"] == [{"property": "转写完成时间", "direction": "descending"}]
    assert mindmap_view["sorts"] == [{"property": "总结完成时间", "direction": "descending"}]


def test_initializer_sanitizes_non_ai_view_and_preserves_valid_additions() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    result = initializer.initialize(create_home=True)

    statistics_view = next(view for view in fake.views.values() if view["name"] == "总收听时长")
    all_property_ids = result.resources["all"].property_ids
    custom_configuration = {
        "type": "table",
        "properties": [
            {"property_id": all_property_ids["Period Key"], "visible": False},
            {"property_id": "removed-property", "visible": True},
        ],
        "wrap_cells": False,
        "frozen_column_index": 1,
        "show_vertical_lines": True,
    }
    statistics_view["configuration"] = custom_configuration

    initializer.initialize()

    desired_spec = next(spec for spec in VIEW_SPECS if spec.name == "总收听时长")
    actual_properties = statistics_view["configuration"]["properties"]
    actual_ids = {item["property_id"] for item in actual_properties}
    desired_ids = {
        item["property_id"]
        for item in view_configuration(desired_spec, all_property_ids)["properties"]
    }
    assert desired_ids.issubset(actual_ids)
    assert all_property_ids["Period Key"] in actual_ids
    assert "removed-property" not in actual_ids
    assert statistics_view["configuration"]["wrap_cells"] is False


def test_view_configuration_rejects_more_than_notion_limit() -> None:
    spec = replace(
        next(spec for spec in VIEW_SPECS if spec.key == "episodes_transcript"),
        visible_properties=tuple(f"property-{index}" for index in range(101)),
    )
    property_ids = {name: f"id-{index}" for index, name in enumerate(spec.visible_properties)}

    with pytest.raises(ValueError, match="allows at most 100"):
        view_configuration(spec, property_ids)


def test_initializer_rejects_more_than_notion_limit_after_stale_cleanup() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize(create_home=True)
    data_source_id = first.resources["episode"].data_source_id
    for index in range(101):
        fake.data_sources[data_source_id]["properties"][f"Custom {index}"] = {
            "id": f"custom-{index}",
            "rich_text": {},
        }
    transcript_view = next(view for view in fake.views.values() if view["name"] == "转写文本")
    transcript_view["configuration"] = {
        "type": "table",
        "properties": [{"property_id": f"custom-{index}", "visible": True} for index in range(101)],
    }

    with pytest.raises(ValueError, match="valid configuration properties"):
        initializer.initialize()


def test_sanitize_view_configuration_rebuilds_malformed_configuration() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    result = initializer.initialize(create_home=True)
    spec = next(spec for spec in VIEW_SPECS if spec.key == "episodes_transcript")
    source = result.resources["episode"]
    expected = view_configuration(spec, source.property_ids)

    assert initializer._sanitize_view_configuration(spec, source, {}) == expected
    assert (
        initializer._sanitize_view_configuration(
            spec,
            source,
            {"configuration": {"properties": {"not": "a-list"}}},
        )
        == expected
    )


def test_view_configuration_count_details_maps_property_ids_to_names() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    result = initializer.initialize(create_home=True)

    transcript_view = next(view for view in fake.views.values() if view["name"] == "转写文本")
    episode_properties = fake.data_sources[result.resources["episode"].data_source_id]["properties"]
    transcript_view["configuration"] = {
        "type": "table",
        "properties": [
            {"property_id": episode_properties["Name"]["id"], "visible": True},
            {"property_id": "removed-property-id", "visible": False},
            "malformed-entry",
        ],
    }

    count_rows = initializer.view_configuration_counts()
    count_transcript_row = next(row for row in count_rows if row["name"] == "转写文本")
    rows = initializer.view_configuration_counts(include_properties=True)
    transcript_row = next(row for row in rows if row["name"] == "转写文本")

    assert count_transcript_row["properties_count"] == 3
    assert count_transcript_row["visible_properties_count"] == 1
    assert count_transcript_row["known_properties_count"] is None
    assert count_transcript_row["unknown_properties_count"] is None
    assert count_transcript_row["properties"] == []
    assert transcript_row["properties_count"] == 3
    assert transcript_row["visible_properties_count"] == 1
    assert transcript_row["known_properties_count"] == 1
    assert transcript_row["unknown_properties_count"] == 2
    assert transcript_row["properties"] == [
        "Name",
        "<unknown:removed-property-id>",
        "<unknown:>",
    ]


def test_initializer_migrates_legacy_mindmap_view_to_episode_source() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize(create_home=True)
    current_view_id = next(
        view_id for view_id, view in fake.views.items() if view["name"] == "AI总结与思维导图"
    )
    ai_database_id = fake.views[current_view_id]["parent"]["database_id"]
    del fake.views[current_view_id]
    legacy_view = fake.create_view(
        {
            "data_source_id": first.resources["mindmap"].data_source_id,
            "name": "AI总结与思维导图",
            "view_type": "table",
            "database_id": ai_database_id,
            "filter": None,
            "sorts": [],
            "configuration": {"type": "table", "properties": []},
        }
    )

    result = initializer.initialize()

    assert result.created_views == 1
    assert result.deleted_views == 1
    assert legacy_view["id"] in fake.deleted_view_ids
    assert (
        sum(
            view["name"] == "AI总结与思维导图"
            and view["data_source_id"] == first.resources["mindmap"].data_source_id
            for view in fake.views.values()
        )
        == 0
    )
    assert sum(view["name"] == "AI总结与思维导图" for view in fake.views.values()) == 1
    transcript_view = next(view for view in fake.views.values() if view["name"] == "转写文本")
    assert transcript_view["parent"]["database_id"] == ai_database_id
    migrated_view = next(view for view in fake.views.values() if view["name"] == "AI总结与思维导图")
    assert migrated_view["data_source_id"] == first.resources["episode"].data_source_id


def test_initializer_rebuilds_missing_view_in_existing_linked_database() -> None:
    fake = FakeNotion()
    initializer = NotionInitializer(fake, "root")
    first = initializer.initialize(create_home=True)
    episode_data_source_id = first.resources["episode"].data_source_id
    missing_id = next(
        view_id for view_id, view in fake.views.items() if view["name"] == "Episode · 在听"
    )
    episode_database_id = str(fake.views[missing_id]["parent"]["database_id"])
    del fake.views[missing_id]
    child_databases_before = [
        block for block in fake.blocks["root"] if block["type"] == "child_database"
    ]

    rebuilt = initializer.initialize()

    assert rebuilt.created_views == 1
    assert rebuilt.updated_views == len(VIEW_SPECS) - 2
    replacement = next(view for view in fake.views.values() if view["name"] == "Episode · 在听")
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


def test_initializer_does_not_duplicate_home_when_marker_link_is_omitted() -> None:
    fake = FakeNotion()
    existing = home_blocks()
    existing[-1] = {
        "id": "marker-without-rich-text",
        "type": "paragraph",
        "paragraph": {"rich_text": []},
    }
    fake.append_block_children("root", existing)
    root_count = len(fake.blocks["root"])

    result = NotionInitializer(fake, "root").initialize(create_home=True)

    assert result.created_home is False
    assert len(fake.blocks["root"]) == root_count + 5


def test_initializer_does_not_duplicate_reshaped_home_without_marker() -> None:
    fake = FakeNotion()
    existing = home_blocks()
    reshaped = [
        existing[0],
        existing[1],
        existing[2],
        {
            "id": "interleaved-root-block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text("保留的用户内容")},
        },
        existing[3],
        existing[4],
    ]
    fake.append_block_children("root", reshaped)
    root_count = len(fake.blocks["root"])

    result = NotionInitializer(fake, "root").initialize(create_home=True)

    assert result.created_home is False
    assert len(fake.blocks["root"]) == root_count + 5


def test_initializer_refuses_home_bootstrap_on_page_with_user_content() -> None:
    fake = FakeNotion()
    user_block = {
        "id": "user-content",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text("不要覆盖我的页面")},
    }
    fake.blocks["root"].append(user_block)

    result = NotionInitializer(fake, "root").initialize(create_home=True)

    assert result.created_home is False
    assert user_block in fake.blocks["root"]
    assert not any(HOME_MARKER_URL in str(block) for block in fake.blocks["root"])


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
        0.28,
        0.72,
    ]
    rendered = str(blocks)
    assert "播客记录" in rendered
    assert "年度热力图每日更新" in rendered
    assert "'type': 'table_of_contents'" in rendered
    assert "待听 · 在听 · 听过 · 喜欢 · 收藏" in rendered
    assert rendered.index("Podcast") < rendered.index("Episode") < rendered.index("转写与总结")
    assert HOME_SUMMARY_MARKER_URL in rendered
    assert "总收听时长" not in rendered
    assert rendered.count("播客记录") == 1


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
    spec = next(spec for spec in VIEW_SPECS if spec.key == "episodes_playlist")
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
    assert spec.filter == {"property": "In Playlist", "checkbox": {"equals": True}}


def test_chart_configuration_uses_dates_hours_and_compact_presentation() -> None:
    month = next(spec for spec in VIEW_SPECS if spec.key == "months_chart")
    configuration = view_configuration(
        month,
        {
            "Start Date": "start",
            "收听小时": "hours",
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
        "axis_labels": "none",
        "grid_lines": "horizontal",
    }

    total = next(spec for spec in VIEW_SPECS if spec.key == "total_time_chart")
    assert view_configuration(total, {"收听小时": "hours"}) == {
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
    ordered_episode_specs = [
        spec for spec in VIEW_SPECS if spec.source == "episode" and spec.home_group is None
    ]
    assert [spec.name for spec in ordered_episode_specs] == [
        "Episode · 待听",
        "Episode · 在听",
        "Episode · 听过",
        "Episode · 喜欢",
        "Episode · 收藏",
    ]
    episode_specs = {spec.key: spec for spec in ordered_episode_specs}
    assert set(episode_specs) == {
        "episodes_listening",
        "episodes_played",
        "episodes_liked",
        "episodes_playlist",
        "episodes_favorited",
    }
    for spec in episode_specs.values():
        assert "Skip AI" not in spec.visible_properties
        assert spec.visible_properties == (
            "Name",
            "Podcast",
            "Listening Status",
            "ASR Status",
            "Progress Ring",
            "Published At",
        )

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


def test_ai_views_are_separate_from_native_episode_status_tabs() -> None:
    transcript = next(spec for spec in VIEW_SPECS if spec.key == "episodes_transcript")
    mindmap = next(spec for spec in VIEW_SPECS if spec.key == "mindmaps")
    assert transcript.home_group == "ai"
    assert mindmap.home_group == "ai"
    assert transcript.name == "转写文本"
    assert mindmap.name == "AI总结与思维导图"
    assert mindmap.aliases == ("思维导图",)
    assert transcript.sorts == ({"property": "转写完成时间", "direction": "descending"},)
    assert mindmap.visible_properties == (
        "Name",
        "人工请求重试",
        "Podcast",
        "增强状态",
        "增强 Provider",
        "总结完成时间",
        "Content Version",
    )
    assert mindmap.source == "episode"
    assert mindmap.filter == {
        "or": [
            {"property": "增强状态", "select": {"equals": "已完成"}},
            {"property": "增强状态", "select": {"equals": "可重试失败"}},
            {"property": "增强状态", "select": {"equals": "最终失败"}},
            {"property": "ASR Status", "select": {"equals": "已增强"}},
            {"property": "ASR Status", "select": {"equals": "已发布"}},
            {"property": "ASR Status", "select": {"equals": "可重试失败"}},
            {"property": "ASR Status", "select": {"equals": "最终失败"}},
        ]
    }
    assert mindmap.sorts == ({"property": "总结完成时间", "direction": "descending"},)
