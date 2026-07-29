import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from xyz2notion.models import ProviderErrorCategory, ProviderFailure
from xyz2notion.state import (
    InvalidStateTransitionError,
    PipelineRecord,
    PipelineState,
    PipelineStateStore,
    StateStoreError,
)

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
FAILURE = ProviderFailure(
    provider="siliconflow",
    category=ProviderErrorCategory.TIMEOUT,
    message="provider timed out",
)


def test_complete_pipeline_visits_all_success_states() -> None:
    record = PipelineRecord(eid="episode-1", updated_at=NOW)
    states = (
        PipelineState.ASR_SUBMITTED,
        PipelineState.ASR_RUNNING,
        PipelineState.TRANSCRIBED,
        PipelineState.ENRICHED,
        PipelineState.PUBLISHED,
    )
    for state in states:
        record = record.transition(state, occurred_at=NOW)
    assert record.state is PipelineState.PUBLISHED
    assert [event.to_state for event in record.history] == list(states)
    assert record.transition(PipelineState.PUBLISHED) is record


def test_direct_import_can_start_from_transcribed() -> None:
    record = PipelineRecord(eid="episode-1").transition(PipelineState.TRANSCRIBED)
    assert record.state is PipelineState.TRANSCRIBED


def test_illegal_transition_is_rejected() -> None:
    record = PipelineRecord(eid="episode-1")
    with pytest.raises(InvalidStateTransitionError, match="Illegal transition"):
        record.transition(PipelineState.PUBLISHED)
    with pytest.raises(InvalidStateTransitionError, match="Only FAILED_RETRYABLE"):
        record.resume()


def test_failure_details_are_required_and_scoped() -> None:
    record = PipelineRecord(eid="episode-1")
    with pytest.raises(InvalidStateTransitionError, match="requires failure"):
        record.transition(PipelineState.FAILED_RETRYABLE)
    with pytest.raises(InvalidStateTransitionError, match="only valid"):
        record.transition(PipelineState.ASR_SUBMITTED, failure=FAILURE)
    authentication_failure = ProviderFailure(
        provider="siliconflow",
        category=ProviderErrorCategory.AUTHENTICATION,
        message="invalid key",
    )
    with pytest.raises(InvalidStateTransitionError, match="cannot enter"):
        record.transition(PipelineState.FAILED_RETRYABLE, failure=authentication_failure)
    failed = record.transition(PipelineState.FAILED_FINAL, failure=FAILURE)
    assert failed.state is PipelineState.FAILED_FINAL
    with pytest.raises(InvalidStateTransitionError):
        failed.transition(PipelineState.DISCOVERED)


def test_retryable_failure_resumes_exact_state() -> None:
    running = (
        PipelineRecord(eid="episode-1")
        .transition(PipelineState.ASR_SUBMITTED)
        .transition(PipelineState.ASR_RUNNING)
    )
    failed = running.transition(PipelineState.FAILED_RETRYABLE, failure=FAILURE)
    assert failed.resume_state is PipelineState.ASR_RUNNING
    assert failed.failure == FAILURE
    resumed = failed.resume(occurred_at=NOW)
    assert resumed.state is PipelineState.ASR_RUNNING
    assert resumed.failure is None
    assert resumed.resume_state is None
    assert resumed.attempts == 1


@pytest.mark.parametrize(
    "values",
    [
        {"eid": "e1", "state": "FAILED_FINAL"},
        {
            "eid": "e1",
            "state": "DISCOVERED",
            "failure": FAILURE.model_dump(mode="json"),
        },
        {
            "eid": "e1",
            "state": "FAILED_RETRYABLE",
            "failure": FAILURE.model_dump(mode="json"),
        },
        {"eid": "e1", "state": "DISCOVERED", "resume_state": "ASR_RUNNING"},
    ],
)
def test_inconsistent_persisted_failure_state_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PipelineRecord.model_validate(values)


def test_state_store_round_trip_resumes_after_interruption(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = PipelineStateStore(path)
    assert store.load() == {}
    running = (
        PipelineRecord(eid="episode-1")
        .transition(PipelineState.ASR_SUBMITTED)
        .transition(PipelineState.ASR_RUNNING)
    )
    store.save({running.idempotency_key: running})
    restored = store.load()[running.idempotency_key]
    assert restored == running
    completed = restored.transition(PipelineState.TRANSCRIBED)
    store.save({completed.idempotency_key: completed})
    assert store.load()[completed.idempotency_key].state is PipelineState.TRANSCRIBED


def test_state_store_rejects_key_mismatch(tmp_path: Path) -> None:
    store = PipelineStateStore(tmp_path / "state.json")
    with pytest.raises(StateStoreError, match="key mismatch"):
        store.save({"episode:wrong": PipelineRecord(eid="right")})


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"schema_version": 2, "records": []}',
        '{"schema_version": 1, "records": {}}',
        '{"schema_version": 1, "records": [{"eid": "e1"}, {"eid": "e1"}]}',
    ],
)
def test_state_store_rejects_untrusted_content(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(StateStoreError):
        PipelineStateStore(path).load()


def test_state_file_is_deterministic_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = PipelineStateStore(path)
    first = PipelineRecord(eid="b")
    second = PipelineRecord(eid="a")
    store.save({first.idempotency_key: first, second.idempotency_key: second})
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [item["eid"] for item in raw["records"]] == ["a", "b"]
