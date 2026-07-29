"""Advance Episode AI work exactly once per persisted Notion checkpoint."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from xyz2notion.asr.pipeline import transcribe_siliconflow_episode
from xyz2notion.asr.router import tingwu_fallback_allowed
from xyz2notion.asr.siliconflow import SiliconFlowClient
from xyz2notion.asr.tingwu import (
    TingwuClient,
    TingwuEnrichment,
    TingwuTask,
    TingwuTaskState,
)
from xyz2notion.enrichment.native import normalize_tingwu_enrichment
from xyz2notion.enrichment.pipeline import SummaryPolicy, TranscriptEnricher
from xyz2notion.enrichment.siliconflow import SiliconFlowSummaryClient
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
)
from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.episode_page import (
    EpisodePageInput,
    EpisodePageRenderer,
)
from xyz2notion.orchestration.state_store import (
    EpisodeAIState,
    NotionEpisodeStateStore,
)
from xyz2notion.state import PipelineState


@dataclass(frozen=True)
class EpisodeCandidate:
    page_id: str
    eid: str
    title: str
    audio_url: str


@dataclass(frozen=True)
class ProcessingOutcome:
    eid: str
    action: str
    state: PipelineState
    detail: str = ""


def _property_text(properties: Mapping[str, Any], name: str) -> str:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return ""
    values = value.get("title") if "title" in value else value.get("rich_text")
    if not isinstance(values, list):
        return ""
    return "".join(
        str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        for item in values
        if isinstance(item, Mapping)
    )


def _property_url(properties: Mapping[str, Any], name: str) -> str:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return ""
    url = value.get("url")
    return str(url) if url else ""


def _property_selection(properties: Mapping[str, Any], name: str) -> str:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return ""
    selected = value.get("select") or value.get("status")
    if not isinstance(selected, Mapping):
        return ""
    return str(selected.get("name") or "")


def _property_number(properties: Mapping[str, Any], name: str) -> float:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return 0
    number = value.get("number")
    return float(number) if isinstance(number, int | float) else 0


def _property_checkbox(properties: Mapping[str, Any], name: str) -> bool:
    value = properties.get(name)
    return bool(value.get("checkbox")) if isinstance(value, Mapping) else False


def episode_candidates(pages: list[JsonObject]) -> tuple[EpisodeCandidate, ...]:
    """Extract processable rows without exposing titles in logs."""
    result: list[EpisodeCandidate] = []
    for page in pages:
        properties = page.get("properties")
        if not isinstance(properties, Mapping) or not page.get("id"):
            continue
        eid = _property_text(properties, "EID")
        title = _property_text(properties, "Name")
        audio_url = _property_url(properties, "Audio URL")
        asr_status = _property_selection(properties, "ASR Status")
        played_seconds = _property_number(properties, "Played Seconds")
        favorited = _property_checkbox(properties, "Favorited")
        skip_ai = _property_checkbox(properties, "Skip AI")
        if (
            eid
            and title
            and audio_url
            and (played_seconds >= 120 or favorited)
            and not skip_ai
            and asr_status not in {"已发布", "最终失败"}
        ):
            result.append(EpisodeCandidate(str(page["id"]), eid, title, audio_url))
    return tuple(result)


def _failure(category: ProviderErrorCategory, message: str) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="xyz2notion",
            category=category,
            message=message,
        )
    )


class EpisodeAIProcessor:
    """Coordinate providers while persisting after every billable boundary."""

    def __init__(
        self,
        notion: Any,
        state_store: NotionEpisodeStateStore,
        *,
        tingwu: TingwuClient | None = None,
        siliconflow: SiliconFlowClient | None = None,
        summary_client: SiliconFlowSummaryClient | None = None,
        summary_policy: SummaryPolicy | None = None,
        summary_enabled: bool = True,
        tingwu_directory: str = "Xyz2Notion 播客",
    ) -> None:
        self.notion = notion
        self.state_store = state_store
        self.tingwu = tingwu
        self.siliconflow = siliconflow
        self.summary_client = summary_client
        self.summary_policy = summary_policy or SummaryPolicy()
        self.summary_enabled = summary_enabled
        self.tingwu_directory = tingwu_directory

    def _save(self, page_id: str, state: EpisodeAIState) -> EpisodeAIState:
        return self.state_store.save(page_id, state)

    def _siliconflow(self, candidate: EpisodeCandidate) -> TranscriptResult:
        if self.siliconflow is None:
            raise _failure(
                ProviderErrorCategory.UNSUPPORTED,
                "No usable ASR provider is configured",
            )
        return transcribe_siliconflow_episode(candidate.audio_url, self.siliconflow)

    def _tingwu_task(
        self,
        candidate: EpisodeCandidate,
        state: EpisodeAIState,
    ) -> TingwuTask:
        if self.tingwu is None:
            raise _failure(
                ProviderErrorCategory.UNSUPPORTED,
                "Tingwu Cookie provider is not configured",
            )
        return self.tingwu.submit_episode(
            self.tingwu_directory,
            candidate.title,
            candidate.audio_url,
            source_task_id=state.source_task_id,
        )

    def _transcript_from_tingwu(
        self,
        task: TingwuTask,
    ) -> tuple[TranscriptResult, TingwuEnrichment | None]:
        if self.tingwu is None:
            raise AssertionError("Tingwu task cannot complete without a client")
        transcript = self.tingwu.get_transcript(task.provider_task_id)
        try:
            native = self.tingwu.get_enrichment(task.provider_task_id)
        except ProviderError:
            native = None
        return transcript, native

    def _advance_asr(
        self,
        candidate: EpisodeCandidate,
        state: EpisodeAIState,
    ) -> tuple[EpisodeAIState, bool]:
        """Return updated state and whether the task is still asynchronous."""
        use_tingwu = state.provider in {None, "tingwu_cookie"} and self.tingwu is not None
        if use_tingwu:
            try:
                task = self._tingwu_task(candidate, state)
            except ProviderError as exc:
                if self.siliconflow is None or not tingwu_fallback_allowed(exc):
                    raise
            else:
                checkpoint = state.model_copy(
                    update={
                        "provider": "tingwu_cookie",
                        "provider_task_id": task.provider_task_id,
                        "source_task_id": task.source_task_id or state.source_task_id,
                        "tingwu_directory_id": task.directory_id,
                        "tingwu_title": task.title,
                    }
                )
                if task.state in {
                    TingwuTaskState.SOURCE_PARSING,
                    TingwuTaskState.SUBMITTED,
                }:
                    record = checkpoint.record
                    if record.state is PipelineState.DISCOVERED:
                        record = record.transition(PipelineState.ASR_SUBMITTED)
                    return checkpoint.model_copy(update={"record": record}), True
                if task.state is TingwuTaskState.PROCESSING:
                    record = checkpoint.record
                    if record.state is PipelineState.DISCOVERED:
                        record = record.transition(PipelineState.ASR_SUBMITTED)
                    if record.state is PipelineState.ASR_SUBMITTED:
                        record = record.transition(PipelineState.ASR_RUNNING)
                    return checkpoint.model_copy(update={"record": record}), True
                if task.state is TingwuTaskState.SUCCEEDED:
                    transcript, native = self._transcript_from_tingwu(task)
                    record = checkpoint.record.transition(PipelineState.TRANSCRIBED)
                    summary = normalize_tingwu_enrichment(native) if native is not None else None
                    return (
                        checkpoint.model_copy(
                            update={
                                "record": record,
                                "transcript": transcript,
                                "summary": summary,
                            }
                        ),
                        False,
                    )
                # A terminal Tingwu record may safely switch to SiliconFlow.

        transcript = self._siliconflow(candidate)
        record = state.record.transition(PipelineState.TRANSCRIBED)
        return (
            state.model_copy(
                update={
                    "record": record,
                    "provider": "siliconflow",
                    "provider_task_id": transcript.provider_task_id,
                    "transcript": transcript,
                    "summary": None,
                }
            ),
            False,
        )

    def _fail(
        self,
        candidate: EpisodeCandidate,
        state: EpisodeAIState,
        error: ProviderError,
    ) -> ProcessingOutcome:
        target = (
            PipelineState.FAILED_RETRYABLE
            if error.failure.retryable
            else PipelineState.FAILED_FINAL
        )
        failed = state.model_copy(
            update={"record": state.record.transition(target, failure=error.failure)}
        )
        saved = self._save(candidate.page_id, failed)
        return ProcessingOutcome(
            candidate.eid,
            "failed",
            saved.record.state,
            error.failure.category.value,
        )

    def process(
        self,
        candidate: EpisodeCandidate,
        page: Mapping[str, Any],
        *,
        retry_failed: bool = False,
        only_failed: bool = False,
    ) -> ProcessingOutcome:
        state = self.state_store.load(page, candidate.eid)
        if only_failed and state.record.state is not PipelineState.FAILED_RETRYABLE:
            return ProcessingOutcome(candidate.eid, "skipped", state.record.state)
        if state.record.state is PipelineState.PUBLISHED:
            return ProcessingOutcome(candidate.eid, "skipped", state.record.state)
        if state.record.state is PipelineState.FAILED_FINAL:
            return ProcessingOutcome(candidate.eid, "skipped", state.record.state)
        if state.record.state is PipelineState.FAILED_RETRYABLE:
            if not retry_failed:
                return ProcessingOutcome(candidate.eid, "waiting_retry", state.record.state)
            state = state.model_copy(update={"record": state.record.resume()})
            state = self._save(candidate.page_id, state)

        try:
            if state.record.state in {
                PipelineState.DISCOVERED,
                PipelineState.ASR_SUBMITTED,
                PipelineState.ASR_RUNNING,
            }:
                if self.tingwu is None and self.siliconflow is None:
                    return ProcessingOutcome(
                        candidate.eid,
                        "paused",
                        state.record.state,
                    )
                state, pending = self._advance_asr(candidate, state)
                state = self._save(candidate.page_id, state)
                if pending:
                    return ProcessingOutcome(candidate.eid, "pending", state.record.state)

            if state.record.state is PipelineState.TRANSCRIBED:
                if state.transcript is None:
                    raise _failure(
                        ProviderErrorCategory.SCHEMA_CHANGED,
                        "Persisted TRANSCRIBED state has no transcript",
                    )
                summary = state.summary
                if summary is None:
                    if self.summary_client is None:
                        return ProcessingOutcome(
                            candidate.eid,
                            ("waiting_summary_key" if self.summary_enabled else "summary_paused"),
                            state.record.state,
                        )
                    summary = TranscriptEnricher(
                        self.summary_client,
                        policy=self.summary_policy,
                    ).summarize(state.transcript)
                state = state.model_copy(
                    update={
                        "record": state.record.transition(PipelineState.ENRICHED),
                        "summary": summary,
                    }
                )
                state = self._save(candidate.page_id, state)

            if state.record.state is PipelineState.ENRICHED:
                if state.transcript is None or state.summary is None:
                    raise _failure(
                        ProviderErrorCategory.SCHEMA_CHANGED,
                        "Persisted ENRICHED state is incomplete",
                    )
                published = EpisodePageRenderer(self.notion).publish(
                    EpisodePageInput(
                        page_id=candidate.page_id,
                        audio_url=candidate.audio_url,
                        transcript=state.transcript,
                        summary=state.summary,
                    )
                )
                state = state.model_copy(
                    update={
                        "record": state.record.transition(PipelineState.PUBLISHED),
                        "content_version": published.content_hash,
                    }
                )
                state = self._save(candidate.page_id, state)
                return ProcessingOutcome(candidate.eid, published.action, state.record.state)
            return ProcessingOutcome(candidate.eid, "pending", state.record.state)
        except ProviderError as exc:
            return self._fail(candidate, state, exc)


def build_provider_clients(
    *,
    tingwu_cookie: SecretStr | None,
    siliconflow_asr_api_key: SecretStr | None,
    siliconflow_summary_api_key: SecretStr | None,
    siliconflow_asr_models: tuple[str, ...],
    siliconflow_summary_models: tuple[str, ...],
) -> tuple[TingwuClient | None, SiliconFlowClient | None, SiliconFlowSummaryClient | None]:
    """Construct only explicitly configured user-owned providers."""
    return (
        TingwuClient(tingwu_cookie) if tingwu_cookie is not None else None,
        SiliconFlowClient(siliconflow_asr_api_key, models=siliconflow_asr_models)
        if siliconflow_asr_api_key is not None
        else None,
        SiliconFlowSummaryClient(
            siliconflow_summary_api_key,
            models=siliconflow_summary_models,
        )
        if siliconflow_summary_api_key is not None
        else None,
    )
