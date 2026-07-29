"""Explicit recovery operations that preserve Episode page content."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from xyz2notion.notion.client import JsonObject


class RecoveryAPI(Protocol):
    def query_data_source(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...


@dataclass(frozen=True)
class ResetResult:
    page_id: str
    action: str


def reset_episode_ai(
    api: RecoveryAPI,
    episode_data_source_id: str,
    eid: str,
) -> ResetResult:
    """Clear only managed AI properties so one exact Episode can be regenerated."""
    normalized = eid.strip()
    if not normalized:
        raise ValueError("EID cannot be empty")
    pages = api.query_data_source(
        episode_data_source_id,
        {
            "filter": {
                "property": "EID",
                "rich_text": {"equals": normalized},
            },
            "page_size": 2,
        },
    )
    if not pages:
        raise ValueError("No Episode matches the requested EID")
    if len(pages) > 1:
        raise ValueError("Multiple Episodes match the requested EID")
    page_id = str(pages[0]["id"])
    api.update_page(
        page_id,
        {
            "properties": {
                "AI State File": {"files": []},
                "ASR Provider": {"rich_text": []},
                "ASR Model": {"rich_text": []},
                "ASR Task ID": {"rich_text": []},
                "ASR Source Task ID": {"rich_text": []},
                "ASR Status": {"select": {"name": "待处理"}},
                "ASR Quality": {"rich_text": []},
                "ASR Accuracy": {"number": None},
                "Failure Reason": {"rich_text": []},
                "Content Version": {"rich_text": []},
            }
        },
    )
    return ResetResult(page_id, "reset")
