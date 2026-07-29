from collections.abc import Mapping
from typing import Any

import pytest

from xyz2notion.notion.client import JsonObject
from xyz2notion.orchestration.recovery import reset_episode_ai


class FakeRecoveryAPI:
    def __init__(self, pages: list[JsonObject]) -> None:
        self.pages = pages
        self.payload: Mapping[str, Any] | None = None
        self.updated: tuple[str, Mapping[str, Any]] | None = None

    def query_data_source(
        self,
        _data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        self.payload = payload
        return self.pages

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.updated = (page_id, payload)
        return {"id": page_id}


def test_reset_episode_clears_only_managed_properties() -> None:
    api = FakeRecoveryAPI([{"id": "page"}])
    result = reset_episode_ai(api, "episode-source", "eid-1")
    assert result.action == "reset"
    assert api.payload["filter"]["rich_text"]["equals"] == "eid-1"  # type: ignore[index]
    assert api.updated is not None
    page_id, payload = api.updated
    assert page_id == "page"
    assert payload["properties"]["ASR Status"]["select"]["name"] == "待处理"  # type: ignore[index]
    assert "Name" not in payload["properties"]  # type: ignore[operator]
    assert "EID" not in payload["properties"]  # type: ignore[operator]


@pytest.mark.parametrize("pages", [[], [{"id": "one"}, {"id": "two"}]])
def test_reset_episode_requires_exactly_one_match(pages: list[JsonObject]) -> None:
    with pytest.raises(ValueError):
        reset_episode_ai(FakeRecoveryAPI(pages), "episode-source", "eid")
