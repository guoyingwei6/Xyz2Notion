"""Clean-room Notion database and view specifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class DatabaseSpec:
    """One managed Notion database and its scalar first-pass schema."""

    key: str
    title: str
    icon: str
    properties: JsonObject


@dataclass(frozen=True)
class ViewSpec:
    """One managed view, including saved filter and presentation settings."""

    key: str
    source: str
    name: str
    view_type: Literal["table", "gallery"]
    visible_properties: tuple[str, ...]
    filter: JsonObject | None = None
    sorts: tuple[JsonObject, ...] = ()
    linked_on_home: bool = True
    cover_property: str | None = None


@dataclass(frozen=True)
class NotionResource:
    """Resolved database and data source IDs."""

    database_id: str
    data_source_id: str
    property_ids: dict[str, str] = field(default_factory=dict)


def title_property() -> JsonObject:
    return {"title": {}}


def text_property() -> JsonObject:
    return {"rich_text": {}}


def number_property(format_name: str = "number") -> JsonObject:
    return {"number": {"format": format_name}}


def select_property(options: tuple[tuple[str, str], ...]) -> JsonObject:
    return {
        "select": {
            "options": [{"name": name, "color": color} for name, color in options],
        }
    }


def relation_property(
    target_data_source_id: str,
    *,
    synced_property_name: str | None = None,
) -> JsonObject:
    relation: JsonObject = {"data_source_id": target_data_source_id}
    if synced_property_name:
        relation["dual_property"] = {
            "synced_property_name": synced_property_name,
        }
    else:
        relation["single_property"] = {}
    return {
        "relation": relation,
    }


def rollup_property(
    relation_name: str,
    target_property_name: str,
    function: str,
) -> JsonObject:
    return {
        "rollup": {
            "relation_property_name": relation_name,
            "rollup_property_name": target_property_name,
            "function": function,
        }
    }


def formula_property(expression: str) -> JsonObject:
    return {"formula": {"expression": expression}}


def period_scalar_properties() -> JsonObject:
    return {
        "Name": title_property(),
        "Period Key": text_property(),
        "Start Date": {"date": {}},
        "End Date": {"date": {}},
        "Exact Listening Seconds": number_property(),
        "Podcast Count": number_property(),
        "Played Days": number_property(),
        "Statistics Source": text_property(),
    }


LISTENING_STATUS_OPTIONS = (
    ("未听", "gray"),
    ("在听", "blue"),
    ("听过", "green"),
)
ASR_STATUS_OPTIONS = (
    ("待处理", "gray"),
    ("排队中", "yellow"),
    ("转写中", "blue"),
    ("已转写", "green"),
    ("已增强", "purple"),
    ("已发布", "green"),
    ("可重试失败", "orange"),
    ("最终失败", "red"),
)


DATABASE_SPECS: tuple[DatabaseSpec, ...] = (
    DatabaseSpec(
        key="author",
        title="Author",
        icon="🎙️",
        properties={
            "Name": title_property(),
            "Author ID": text_property(),
            "Avatar": {"files": {}},
            "Bio": text_property(),
            "Updated At": {"date": {}},
        },
    ),
    DatabaseSpec(
        key="podcast",
        title="Podcast",
        icon="🎧",
        properties={
            "Name": title_property(),
            "PID": text_property(),
            "Cover": {"files": {}},
            "Description": text_property(),
            "URL": {"url": {}},
            "Total Listening Seconds": number_property(),
            "Rank": number_property(),
            "Updated At": {"date": {}},
        },
    ),
    DatabaseSpec(
        key="episode",
        title="Episode",
        icon="▶️",
        properties={
            "Name": title_property(),
            "EID": text_property(),
            "Description": text_property(),
            "Cover": {"files": {}},
            "Published At": {"date": {}},
            "Audio URL": {"url": {}},
            "Duration Seconds": number_property(),
            "Played Seconds": number_property(),
            "Progress Ring": text_property(),
            "Listening Status": select_property(LISTENING_STATUS_OPTIONS),
            "Liked": {"checkbox": {}},
            "Favorited": {"checkbox": {}},
            "In Playlist": {"checkbox": {}},
            "Playlist Position": number_property(),
            "Skip AI": {"checkbox": {}},
            "Last Played At": {"date": {}},
            "ASR Provider": text_property(),
            "ASR Model": text_property(),
            "ASR Task ID": text_property(),
            "ASR Source Task ID": text_property(),
            "ASR Status": select_property(ASR_STATUS_OPTIONS),
            "ASR Quality": text_property(),
            "ASR Accuracy": number_property("percent"),
            "Failure Reason": text_property(),
            "Content Version": text_property(),
            "AI State File": {"files": {}},
        },
    ),
    DatabaseSpec(
        key="all",
        title="全部",
        icon="📊",
        properties=period_scalar_properties(),
    ),
    DatabaseSpec(
        key="year",
        title="年",
        icon="🗓️",
        properties=period_scalar_properties(),
    ),
    DatabaseSpec(
        key="month",
        title="月",
        icon="📅",
        properties=period_scalar_properties(),
    ),
    DatabaseSpec(
        key="week",
        title="周",
        icon="📆",
        properties=period_scalar_properties(),
    ),
    DatabaseSpec(
        key="day",
        title="日",
        icon="☀️",
        properties=period_scalar_properties(),
    ),
    DatabaseSpec(
        key="mindmap",
        title="思维导图",
        icon="🧠",
        properties={
            "Name": title_property(),
            "Mindmap JSON": text_property(),
            "Mermaid": text_property(),
            "Content Version": text_property(),
            "Updated At": {"date": {}},
        },
    ),
)


def relational_properties(resources: dict[str, NotionResource]) -> dict[str, JsonObject]:
    """Build second-pass relations, rollups, and formulas after all IDs exist."""
    episode = resources["episode"].data_source_id
    podcast = resources["podcast"].data_source_id
    author = resources["author"].data_source_id
    result: dict[str, JsonObject] = {
        "podcast": {
            "Authors": relation_property(
                author,
                synced_property_name="Podcasts",
            ),
        },
        "episode": {
            "Podcast": relation_property(
                podcast,
                synced_property_name="Episodes",
            ),
            "All Period": relation_property(
                resources["all"].data_source_id,
                synced_property_name="Episodes",
            ),
            "Year Period": relation_property(
                resources["year"].data_source_id,
                synced_property_name="Episodes",
            ),
            "Month Period": relation_property(
                resources["month"].data_source_id,
                synced_property_name="Episodes",
            ),
            "Week Period": relation_property(
                resources["week"].data_source_id,
                synced_property_name="Episodes",
            ),
            "Day Period": relation_property(
                resources["day"].data_source_id,
                synced_property_name="Episodes",
            ),
            "Progress Percent": formula_property(
                'if(prop("Duration Seconds") > 0, '
                'round(prop("Played Seconds") / prop("Duration Seconds") * 100), 0)'
            ),
        },
        "mindmap": {
            "Episode": relation_property(
                episode,
                synced_property_name="Mindmaps",
            ),
        },
    }
    for key in ("all", "year", "month", "week", "day"):
        result[key] = {
            "Episodes": relation_property(episode),
            "Listening Seconds": rollup_property("Episodes", "Played Seconds", "sum"),
            "Episode Count": rollup_property("Episodes", "Name", "count"),
            "Listening Hours": formula_property(
                'round(prop("Exact Listening Seconds") / 3600 * 10) / 10'
            ),
        }
    return result


VIEW_SPECS: tuple[ViewSpec, ...] = (
    ViewSpec(
        key="total_time",
        source="all",
        name="总收听时长",
        view_type="table",
        visible_properties=(
            "Name",
            "Listening Hours",
            "Podcast Count",
            "Episode Count",
            "Played Days",
            "Statistics Source",
        ),
        sorts=({"property": "Listening Seconds", "direction": "descending"},),
    ),
    ViewSpec(
        key="years",
        source="year",
        name="年度统计",
        view_type="table",
        visible_properties=(
            "Name",
            "Listening Hours",
            "Podcast Count",
            "Episode Count",
            "Played Days",
            "Statistics Source",
        ),
        sorts=({"property": "Start Date", "direction": "descending"},),
    ),
    ViewSpec(
        key="months",
        source="month",
        name="月度统计",
        view_type="table",
        visible_properties=(
            "Name",
            "Listening Hours",
            "Podcast Count",
            "Episode Count",
            "Played Days",
            "Statistics Source",
        ),
        sorts=({"property": "Start Date", "direction": "descending"},),
    ),
    ViewSpec(
        key="weeks",
        source="week",
        name="周统计",
        view_type="table",
        visible_properties=(
            "Name",
            "Listening Hours",
            "Podcast Count",
            "Episode Count",
            "Played Days",
            "Statistics Source",
        ),
        sorts=({"property": "Start Date", "direction": "descending"},),
    ),
    ViewSpec(
        key="days",
        source="day",
        name="日统计",
        view_type="table",
        visible_properties=(
            "Name",
            "Listening Hours",
            "Podcast Count",
            "Episode Count",
            "Played Days",
            "Statistics Source",
        ),
        sorts=({"property": "Start Date", "direction": "descending"},),
    ),
    ViewSpec(
        key="ranking",
        source="podcast",
        name="收听时长排行榜",
        view_type="table",
        visible_properties=("Rank", "Name", "Cover", "Total Listening Seconds"),
        filter={
            "property": "Total Listening Seconds",
            "number": {"greater_than": 0},
        },
        sorts=({"property": "Total Listening Seconds", "direction": "descending"},),
    ),
    ViewSpec(
        key="podcasts",
        source="podcast",
        name="Podcast",
        view_type="gallery",
        visible_properties=("Name", "Total Listening Seconds"),
        filter={
            "property": "Total Listening Seconds",
            "number": {"greater_than": 0},
        },
        sorts=({"property": "Updated At", "direction": "descending"},),
        cover_property="Cover",
    ),
    ViewSpec(
        key="episodes_all",
        source="episode",
        name="Episode · 全部",
        view_type="gallery",
        visible_properties=(
            "Name",
            "Listening Status",
            "Progress Ring",
            "Published At",
        ),
        filter={"property": "Played Seconds", "number": {"greater_than": 0}},
        sorts=({"property": "Published At", "direction": "descending"},),
        cover_property="Cover",
    ),
    ViewSpec(
        key="episodes_listening",
        source="episode",
        name="Episode · 在听",
        view_type="gallery",
        visible_properties=("Name", "Listening Status", "Progress Ring", "Last Played At"),
        filter={"property": "Listening Status", "select": {"equals": "在听"}},
        sorts=({"property": "Last Played At", "direction": "descending"},),
        cover_property="Cover",
    ),
    ViewSpec(
        key="episodes_played",
        source="episode",
        name="Episode · 听过",
        view_type="gallery",
        visible_properties=("Name", "Listening Status", "Progress Ring", "Published At"),
        filter={"property": "Listening Status", "select": {"equals": "听过"}},
        sorts=({"property": "Last Played At", "direction": "descending"},),
        cover_property="Cover",
    ),
    ViewSpec(
        key="episodes_liked",
        source="episode",
        name="Episode · 喜欢",
        view_type="gallery",
        visible_properties=("Name", "Listening Status", "Progress Ring", "Published At"),
        filter={
            "and": [
                {"property": "Played Seconds", "number": {"greater_than": 0}},
                {"property": "Liked", "checkbox": {"equals": True}},
            ]
        },
        sorts=({"property": "Published At", "direction": "descending"},),
        cover_property="Cover",
    ),
    ViewSpec(
        key="episodes_playlist",
        source="episode",
        name="Episode · 待听",
        view_type="gallery",
        visible_properties=("Name", "Listening Status", "Progress Ring", "Published At"),
        filter={"property": "In Playlist", "checkbox": {"equals": True}},
        sorts=(
            {"property": "Playlist Position", "direction": "ascending"},
            {"property": "Published At", "direction": "descending"},
        ),
        cover_property="Cover",
    ),
    ViewSpec(
        key="episodes_favorited",
        source="episode",
        name="Episode · 收藏",
        view_type="gallery",
        visible_properties=("Name", "Listening Status", "Progress Ring", "Published At"),
        filter={"property": "Favorited", "checkbox": {"equals": True}},
        sorts=({"property": "Published At", "direction": "descending"},),
        cover_property="Cover",
    ),
    ViewSpec(
        key="mindmaps",
        source="mindmap",
        name="思维导图",
        view_type="table",
        visible_properties=("Name", "Episode", "Content Version", "Updated At"),
        sorts=({"property": "Updated At", "direction": "descending"},),
    ),
)


def view_configuration(spec: ViewSpec, property_ids: dict[str, str]) -> JsonObject:
    """Resolve property names to IDs required by the 2026 Views API."""
    properties = [
        {"property_id": property_ids[name], "visible": True}
        for name in spec.visible_properties
        if name in property_ids
    ]
    if spec.view_type == "gallery":
        configuration: JsonObject = {
            "type": "gallery",
            "properties": properties,
            "cover": {"type": "page_cover"},
            "cover_size": "medium",
            "cover_aspect": "cover",
            "card_layout": "list",
        }
        if spec.cover_property and spec.cover_property in property_ids:
            configuration["cover"] = {
                "type": "property",
                "property_id": property_ids[spec.cover_property],
            }
        return configuration
    return {
        "type": "table",
        "properties": properties,
        "wrap_cells": True,
        "frozen_column_index": 1,
        "show_vertical_lines": False,
    }
