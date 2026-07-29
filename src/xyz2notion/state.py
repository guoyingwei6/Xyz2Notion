"""Recoverable episode pipeline state machine and JSON store."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from xyz2notion.models import ContractModel, ProviderFailure, episode_key, utc_now


class InvalidStateTransitionError(ValueError):
    """Raised when a pipeline transition violates the state graph."""


class StateStoreError(ValueError):
    """Raised when persisted pipeline state cannot be trusted."""


class PipelineState(StrEnum):
    """Persisted processing states for an episode."""

    DISCOVERED = "DISCOVERED"
    ASR_SUBMITTED = "ASR_SUBMITTED"
    ASR_RUNNING = "ASR_RUNNING"
    TRANSCRIBED = "TRANSCRIBED"
    ENRICHED = "ENRICHED"
    PUBLISHED = "PUBLISHED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.DISCOVERED: frozenset(
        {
            PipelineState.ASR_SUBMITTED,
            PipelineState.TRANSCRIBED,
            PipelineState.FAILED_RETRYABLE,
            PipelineState.FAILED_FINAL,
        }
    ),
    PipelineState.ASR_SUBMITTED: frozenset(
        {
            PipelineState.ASR_RUNNING,
            PipelineState.TRANSCRIBED,
            PipelineState.FAILED_RETRYABLE,
            PipelineState.FAILED_FINAL,
        }
    ),
    PipelineState.ASR_RUNNING: frozenset(
        {
            PipelineState.TRANSCRIBED,
            PipelineState.FAILED_RETRYABLE,
            PipelineState.FAILED_FINAL,
        }
    ),
    PipelineState.TRANSCRIBED: frozenset(
        {
            PipelineState.ENRICHED,
            PipelineState.FAILED_RETRYABLE,
            PipelineState.FAILED_FINAL,
        }
    ),
    PipelineState.ENRICHED: frozenset(
        {
            PipelineState.PUBLISHED,
            PipelineState.FAILED_RETRYABLE,
            PipelineState.FAILED_FINAL,
        }
    ),
    PipelineState.PUBLISHED: frozenset(),
    PipelineState.FAILED_RETRYABLE: frozenset(),
    PipelineState.FAILED_FINAL: frozenset(),
}


class TransitionEvent(ContractModel):
    """Auditable state transition."""

    from_state: PipelineState
    to_state: PipelineState
    occurred_at: AwareDatetime = Field(default_factory=utc_now)


class PipelineRecord(ContractModel):
    """Current persisted state and transition history for one episode."""

    eid: str = Field(min_length=1)
    state: PipelineState = PipelineState.DISCOVERED
    resume_state: PipelineState | None = None
    attempts: int = Field(default=0, ge=0)
    failure: ProviderFailure | None = None
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    history: tuple[TransitionEvent, ...] = ()

    @model_validator(mode="after")
    def validate_failure_state(self) -> Self:
        failed = self.state in {PipelineState.FAILED_RETRYABLE, PipelineState.FAILED_FINAL}
        if failed and self.failure is None:
            raise ValueError("failed pipeline states require failure details")
        if not failed and self.failure is not None:
            raise ValueError("non-failed pipeline states cannot retain failure details")
        if self.state is PipelineState.FAILED_RETRYABLE and self.resume_state is None:
            raise ValueError("FAILED_RETRYABLE requires resume_state")
        if self.state is not PipelineState.FAILED_RETRYABLE and self.resume_state is not None:
            raise ValueError("resume_state is only valid for FAILED_RETRYABLE")
        return self

    @property
    def idempotency_key(self) -> str:
        return episode_key(self.eid)

    def transition(
        self,
        target: PipelineState,
        *,
        failure: ProviderFailure | None = None,
        occurred_at: datetime | None = None,
    ) -> PipelineRecord:
        """Return a new record after one legal transition."""
        if target is self.state:
            return self
        if target not in _TRANSITIONS[self.state]:
            raise InvalidStateTransitionError(f"Illegal transition: {self.state} -> {target}")
        is_failure = target in {PipelineState.FAILED_RETRYABLE, PipelineState.FAILED_FINAL}
        if is_failure and failure is None:
            raise InvalidStateTransitionError(f"{target} requires failure details")
        if not is_failure and failure is not None:
            raise InvalidStateTransitionError("Failure details are only valid for failed states")
        if (
            target is PipelineState.FAILED_RETRYABLE
            and failure is not None
            and not failure.retryable
        ):
            raise InvalidStateTransitionError(
                f"{failure.category} failures cannot enter FAILED_RETRYABLE"
            )
        timestamp = occurred_at or utc_now()
        return PipelineRecord(
            eid=self.eid,
            state=target,
            resume_state=self.state if target is PipelineState.FAILED_RETRYABLE else None,
            attempts=self.attempts,
            failure=failure,
            updated_at=timestamp,
            history=(
                *self.history,
                TransitionEvent(
                    from_state=self.state,
                    to_state=target,
                    occurred_at=timestamp,
                ),
            ),
        )

    def resume(self, *, occurred_at: datetime | None = None) -> PipelineRecord:
        """Resume the exact state that failed without restarting completed work."""
        if self.state is not PipelineState.FAILED_RETRYABLE or self.resume_state is None:
            raise InvalidStateTransitionError("Only FAILED_RETRYABLE records can resume")
        timestamp = occurred_at or utc_now()
        return PipelineRecord(
            eid=self.eid,
            state=self.resume_state,
            attempts=self.attempts + 1,
            updated_at=timestamp,
            history=(
                *self.history,
                TransitionEvent(
                    from_state=self.state,
                    to_state=self.resume_state,
                    occurred_at=timestamp,
                ),
            ),
        )


class PipelineStateStore:
    """Atomic JSON persistence for resumable GitHub Actions runs."""

    schema_version = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, PipelineRecord]:
        """Load records, returning an empty store when no state file exists."""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise StateStoreError("Pipeline state root must be an object")
            if raw.get("schema_version") != self.schema_version:
                raise StateStoreError("Unsupported pipeline state schema_version")
            record_items = raw.get("records", [])
            if not isinstance(record_items, list):
                raise StateStoreError("Pipeline state records must be an array")
            records = [PipelineRecord.model_validate(item) for item in record_items]
        except StateStoreError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StateStoreError(f"Cannot load pipeline state: {self.path}") from exc
        indexed = {record.idempotency_key: record for record in records}
        if len(indexed) != len(records):
            raise StateStoreError("Pipeline state contains duplicate episode records")
        return indexed

    def save(self, records: dict[str, PipelineRecord]) -> None:
        """Atomically replace the state file after validating record keys."""
        for key, record in records.items():
            if key != record.idempotency_key:
                raise StateStoreError(f"Pipeline state key mismatch: {key}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "records": [
                record.model_dump(mode="json")
                for _, record in sorted(records.items(), key=lambda item: item[0])
            ],
        }
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                temporary_path = Path(handle.name)
            temporary_path.replace(self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StateStoreError(f"Cannot save pipeline state: {self.path}") from exc
