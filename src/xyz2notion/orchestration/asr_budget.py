"""Persist conservative ASR reservations across serialized queue runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol

from xyz2notion.config import LimitConfig
from xyz2notion.models import local_date, local_today
from xyz2notion.notion.client import JsonObject, NotionAPIError, rich_text

ASR_USAGE_PROPERTY = "ASR Usage Ledger"


class AsrDeferredError(RuntimeError):
    """No new ASR call is permitted until its duration or budget is available."""


class UsageAPI(Protocol):
    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...


def _usage(page: Mapping[str, Any]) -> dict[str, int]:
    properties = page.get("properties", {})
    value = properties.get(ASR_USAGE_PROPERTY, {})
    text = "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in value.get("rich_text", [])
    )
    if not text:
        # Older completed checkpoints have no reservations. Count their visible
        # duration on the completion date without changing the original checkpoint.
        completed = properties.get("转写完成时间", {}).get("date") or {}
        duration = properties.get("Duration Seconds", {}).get("number")
        if completed.get("start") and isinstance(duration, int | float) and duration > 0:
            day = local_date(datetime.fromisoformat(completed["start"].replace("Z", "+00:00")))
            return {day.isoformat(): int(duration)}
        return {}
    try:
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise ValueError("expected object")
        for day, seconds in decoded.items():
            date.fromisoformat(day)
            if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
                raise ValueError("invalid seconds")
        return decoded
    except (TypeError, ValueError) as exc:
        raise NotionAPIError("ASR Usage Ledger is invalid; audit before submitting ASR") from exc


class AsrBudget:
    """Reserve before each provider attempt; failed/ambiguous attempts stay counted."""

    def __init__(
        self,
        api: UsageAPI,
        pages: Sequence[JsonObject],
        limits: LimitConfig,
        *,
        today: Callable[[], date] = local_today,
    ) -> None:
        self.api = api
        self.limits = limits
        self.today = today
        self.ledgers = {str(page["id"]): _usage(page) for page in pages if page.get("id")}

    def reserve(self, page_id: str, duration_seconds: int) -> None:
        if duration_seconds <= 0:
            raise AsrDeferredError("unknown_duration")
        day = self.today().isoformat()
        daily = sum(ledger.get(day, 0) for ledger in self.ledgers.values())
        monthly = sum(
            seconds
            for ledger in self.ledgers.values()
            for key, seconds in ledger.items()
            if key[:7] == day[:7]
        )
        if daily + duration_seconds > self.limits.asr_minutes_per_day * 60:
            raise AsrDeferredError("daily_limit")
        if monthly + duration_seconds > self.limits.asr_minutes_per_month * 60:
            raise AsrDeferredError("monthly_limit")
        ledger = dict(self.ledgers.get(page_id, {}))
        ledger[day] = ledger.get(day, 0) + duration_seconds
        self.api.update_page(
            page_id,
            {
                "properties": {
                    ASR_USAGE_PROPERTY: {
                        "rich_text": rich_text(json.dumps(ledger, separators=(",", ":")))
                    }
                }
            },
        )
        self.ledgers[page_id] = ledger
