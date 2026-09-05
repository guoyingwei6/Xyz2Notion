import json
from datetime import date

import pytest

from xyz2notion.config import LimitConfig
from xyz2notion.notion.client import NotionAPIError
from xyz2notion.orchestration.asr_budget import ASR_USAGE_PROPERTY, AsrBudget, AsrDeferredError


class UsageStore:
    def __init__(self) -> None:
        self.pages = {"one": {"id": "one", "properties": {}}}

    def update_page(self, page_id, payload):
        self.pages[page_id]["properties"].update(payload["properties"])
        return self.pages[page_id]


def test_budget_is_persisted_and_counts_failed_attempts_after_restart() -> None:
    store = UsageStore()
    limits = LimitConfig(asr_minutes_per_day=2, asr_minutes_per_month=3)

    def today() -> date:
        return date(2026, 9, 5)

    budget = AsrBudget(store, list(store.pages.values()), limits, today=today)
    budget.reserve("one", 60)
    resumed = AsrBudget(store, list(store.pages.values()), limits, today=today)
    resumed.reserve("one", 60)
    with pytest.raises(AsrDeferredError, match="daily_limit"):
        resumed.reserve("one", 1)
    tomorrow = AsrBudget(
        store,
        list(store.pages.values()),
        limits,
        today=lambda: date(2026, 9, 6),
    )
    tomorrow.reserve("one", 60)
    with pytest.raises(AsrDeferredError, match="monthly_limit"):
        tomorrow.reserve("one", 1)
    with pytest.raises(AsrDeferredError, match="unknown_duration"):
        tomorrow.reserve("one", 0)


@pytest.mark.parametrize("ledger", ["not-json", "[]", '{"bad":1}', '{"2026-09-05":true}'])
def test_invalid_ledger_stops_new_submissions(ledger: str) -> None:
    page = {
        "id": "one",
        "properties": {
            ASR_USAGE_PROPERTY: {"rich_text": [{"plain_text": ledger}]},
        },
    }
    with pytest.raises(NotionAPIError, match="audit"):
        AsrBudget(UsageStore(), [page], LimitConfig())


def test_old_completed_transcript_counts_toward_current_budget() -> None:
    store = UsageStore()
    store.pages["one"]["properties"] = {
        "转写完成时间": {"date": {"start": "2026-09-04T23:00:00Z"}},
        "Duration Seconds": {"number": 60},
    }
    budget = AsrBudget(
        store,
        list(store.pages.values()),
        LimitConfig(asr_minutes_per_day=1, asr_minutes_per_month=1),
        today=lambda: date(2026, 9, 5),
    )
    with pytest.raises(AsrDeferredError, match="daily_limit"):
        budget.reserve("one", 1)
    assert budget.ledgers == {"one": {"2026-09-05": 60}}


def test_failed_reservation_write_does_not_start_or_advance_usage() -> None:
    class FailingStore(UsageStore):
        def update_page(self, page_id, payload):
            raise NotionAPIError("write failed")

    budget = AsrBudget(FailingStore(), [], LimitConfig(), today=lambda: date(2026, 9, 5))
    with pytest.raises(NotionAPIError):
        budget.reserve("one", 60)
    assert budget.ledgers == {}


def test_previous_month_usage_does_not_block_current_month() -> None:
    store = UsageStore()
    store.pages["one"]["properties"] = {
        ASR_USAGE_PROPERTY: {
            "rich_text": [{"plain_text": json.dumps({"2026-08-31": 99999})}],
        }
    }
    AsrBudget(
        store,
        list(store.pages.values()),
        LimitConfig(),
        today=lambda: date(2026, 9, 1),
    ).reserve("one", 60)
