"""Advance Episode AI work exactly once per persisted Notion checkpoint."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from xyz2notion.asr.audio import AudioPreparationError, validate_public_audio_url
from xyz2notion.asr.dashscope import DashScopeParaformerClient
from xyz2notion.asr.local_whisper import LocalWhisperClient
from xyz2notion.asr.pipeline import transcribe_siliconflow_episode
from xyz2notion.asr.siliconflow import SiliconFlowClient
from xyz2notion.config import AsrProvider
from xyz2notion.enrichment.client import StructuredSummaryClient, chain_summary_clients
from xyz2notion.enrichment.dashscope import DashScopeSummaryClient
from xyz2notion.enrichment.local_qwen import LocalQwenSummaryClient
from xyz2notion.enrichment.pipeline import SummaryPolicy, TranscriptEnricher
from xyz2notion.enrichment.siliconflow import SiliconFlowSummaryClient
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
)
from xyz2notion.notion.client import JsonObject, NotionAPIError
from xyz2notion.notion.episode_page import (
    EpisodePageInput,
    EpisodePagePublishResult,
    EpisodePageRenderer,
)
from xyz2notion.notion.mindmap_database import MindmapDatabaseSynchronizer
from xyz2notion.orchestration.asr_budget import AsrBudget, AsrDeferredError
from xyz2notion.orchestration.state_store import (
    EpisodeAIState,
    NotionEpisodeStateStore,
)
from xyz2notion.state import PipelineState

MAX_RETRY_ATTEMPTS = 3
AI_CATEGORY_LABELS = (
    "favorite",
    "liked",
    "heard",
    "listening",
    "to_listen",
    "other",
)


@dataclass(frozen=True)
class EpisodeCandidate:
    page_id: str
    eid: str
    title: str
    audio_url: str
    duration_seconds: int = 0


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


def ai_category_priority(page: Mapping[str, Any]) -> int:
    """Return the user's preferred AI order for an eligible Episode page.

    The priority is separate from the candidate safety gate below: a row must
    still have at least 120 played seconds, be favorited, or be liked before it
    enters the queue. Overlapping flags use the most important category.
    """
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return 5
    if _property_checkbox(properties, "Favorited"):
        return 0
    if _property_checkbox(properties, "Liked"):
        return 1
    listening_status = _property_selection(properties, "Listening Status")
    if listening_status == "听过":
        return 2
    if listening_status == "在听":
        return 3
    if listening_status == "未听" or _property_checkbox(properties, "In Playlist"):
        return 4
    return 5


def ai_category_label(page: Mapping[str, Any]) -> str:
    """Return a private-safe label for queue telemetry and ordering audits."""
    return AI_CATEGORY_LABELS[ai_category_priority(page)]


def episode_candidates(
    pages: list[JsonObject],
    *,
    include_final: bool = False,
    manual_override: bool = False,
) -> tuple[EpisodeCandidate, ...]:
    """Extract processable rows without exposing titles in logs.

    Final failures stay out of every ordinary queue.  The explicit manual
    queue may opt in to them after reopening the persisted checkpoint. A
    checked manual request can also bypass the automatic 120-second/favorite/
    liked candidate gate while keeping the explicit ``Skip AI`` opt-out.
    """
    result: list[EpisodeCandidate] = []
    for page in pages:
        properties = page.get("properties")
        if not isinstance(properties, Mapping) or not page.get("id"):
            continue
        eid = _property_text(properties, "EID")
        title = _property_text(properties, "Name")
        audio_url = _property_url(properties, "Audio URL")
        asr_status = _property_selection(properties, "ASR Status")
        enrichment_status = _property_selection(properties, "增强状态")
        played_seconds = _property_number(properties, "Played Seconds")
        liked = _property_checkbox(properties, "Liked")
        favorited = _property_checkbox(properties, "Favorited")
        skip_ai = _property_checkbox(properties, "Skip AI")
        if (
            eid
            and title
            and audio_url
            and (manual_override or played_seconds >= 120 or favorited or liked)
            and not skip_ai
            and (
                include_final
                or (
                    asr_status not in {"已发布", "最终失败"}
                    and enrichment_status not in {"已完成", "最终失败"}
                )
            )
        ):
            try:
                validate_public_audio_url(audio_url)
            except AudioPreparationError:
                # A stale/non-public legacy URL must not abort the whole queue;
                # leave the row pending and let a later metadata sync repair it.
                continue
            result.append(
                EpisodeCandidate(
                    str(page["id"]),
                    eid,
                    title,
                    audio_url,
                    int(_property_number(properties, "Duration Seconds")),
                )
            )
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
        dashscope: DashScopeParaformerClient | None = None,
        siliconflow: SiliconFlowClient | None = None,
        local_whisper: LocalWhisperClient | None = None,
        summary_client: StructuredSummaryClient | None = None,
        summary_policy: SummaryPolicy | None = None,
        summary_enabled: bool = True,
        mindmap_data_source_id: str | None = None,
        provider_order: tuple[AsrProvider, ...] = tuple(AsrProvider),
        asr_budget: AsrBudget | None = None,
    ) -> None:
        self.notion = notion
        self.state_store = state_store
        self.dashscope = dashscope
        self.siliconflow = siliconflow
        self.local_whisper = local_whisper
        self.summary_client = summary_client
        self.summary_policy = summary_policy or SummaryPolicy()
        self.summary_enabled = summary_enabled
        self.mindmap_data_source_id = mindmap_data_source_id
        self.provider_order = provider_order
        self.asr_budget = asr_budget
        self._checkpoint: EpisodeAIState | None = None

    def _save(self, page_id: str, state: EpisodeAIState) -> EpisodeAIState:
        saved = self.state_store.save(page_id, state)
        self._checkpoint = saved
        return saved

    def _siliconflow(self, candidate: EpisodeCandidate) -> TranscriptResult:
        if self.siliconflow is None:
            raise _failure(
                ProviderErrorCategory.UNSUPPORTED,
                "No usable ASR provider is configured",
            )
        return transcribe_siliconflow_episode(candidate.audio_url, self.siliconflow)

    @staticmethod
    def _is_sensitive_asr_failure(error: ProviderError) -> bool:
        """Detect provider input inspection so the episode stops without fallback."""
        normalized_code = (error.failure.code or "").lower().replace("_", "").replace("-", "")
        normalized_message = error.failure.message.lower().replace("_", "").replace("-", "")
        return any(
            marker in normalized_code or marker in normalized_message
            for marker in ("datainspectionfailed", "contentfilter", "sensitivecontent")
        )

    def _local_whisper(self, candidate: EpisodeCandidate) -> TranscriptResult:
        if self.local_whisper is None:
            raise _failure(
                ProviderErrorCategory.UNSUPPORTED,
                "No usable local ASR provider is configured",
            )
        return transcribe_siliconflow_episode(candidate.audio_url, self.local_whisper)

    def _publish(
        self,
        candidate: EpisodeCandidate,
        state: EpisodeAIState,
    ) -> EpisodePagePublishResult:
        if state.transcript is None or state.summary is None:
            raise _failure(
                ProviderErrorCategory.SCHEMA_CHANGED,
                "Persisted ENRICHED state is incomplete",
            )
        try:
            published = EpisodePageRenderer(self.notion).publish(
                EpisodePageInput(
                    page_id=candidate.page_id,
                    audio_url=candidate.audio_url,
                    transcript=state.transcript,
                    summary=state.summary,
                )
            )
            if self.mindmap_data_source_id is not None:
                MindmapDatabaseSynchronizer(
                    self.notion,
                    self.mindmap_data_source_id,
                ).sync(
                    eid=candidate.eid,
                    episode_page_id=candidate.page_id,
                    episode_title=candidate.title,
                    summary=state.summary,
                    content_version=published.content_hash,
                )
            return published
        except ProviderError:
            raise
        except (NotionAPIError, RuntimeError) as exc:
            raise ProviderError(
                ProviderFailure(
                    provider="notion_publish",
                    category=(
                        ProviderErrorCategory.UNKNOWN
                        if isinstance(exc, NotionAPIError) and exc.code == "ambiguous_write"
                        else ProviderErrorCategory.UNAVAILABLE
                    ),
                    message=f"Notion episode publishing failed: {type(exc).__name__}",
                    code=exc.code if isinstance(exc, NotionAPIError) else None,
                )
            ) from exc

    def _advance_asr(
        self,
        candidate: EpisodeCandidate,
        state: EpisodeAIState,
    ) -> tuple[EpisodeAIState, bool]:
        """Return updated state and whether the task is still asynchronous."""
        if state.submission_uncertain:
            raise _failure(
                ProviderErrorCategory.UNKNOWN,
                "ASR submission outcome is unknown; audit the provider before an explicit reset",
            )
        if state.record.state in {
            PipelineState.ASR_SUBMITTED,
            PipelineState.ASR_RUNNING,
        }:
            # A persisted remote task is already billable.  Only poll that
            # exact task; never create a second submission for a manual
            # priority request or a retry queue run.  Legacy checkpoints that
            # lack a task id remain pending until an explicit repair can
            # establish a safe resume point.
            if (
                state.provider == "dashscope"
                and state.provider_task_id
                and self.dashscope is not None
            ):
                result_url = self.dashscope.wait_result_url(state.provider_task_id)
                transcript = (
                    self.dashscope.fetch_transcript(
                        result_url,
                        task_id=state.provider_task_id,
                        model=state.provider_model,
                    )
                    if state.provider_model
                    else self.dashscope.fetch_transcript(
                        result_url,
                        task_id=state.provider_task_id,
                    )
                )
                record = state.record.transition(PipelineState.TRANSCRIBED)
                return (
                    state.model_copy(
                        update={
                            "record": record,
                            "provider": transcript.provider,
                            "provider_task_id": transcript.provider_task_id,
                            "transcript": transcript,
                            "summary": None,
                        }
                    ),
                    False,
                )
            return state, True

        clients = {
            AsrProvider.DASHSCOPE: self.dashscope,
            AsrProvider.SILICONFLOW: self.siliconflow,
            AsrProvider.LOCAL_WHISPER: self.local_whisper,
        }
        last_error: ProviderError | None = None
        for provider in self.provider_order:
            if clients[provider] is None:
                continue
            if self.asr_budget is not None:
                self.asr_budget.reserve(candidate.page_id, candidate.duration_seconds)
            if provider is AsrProvider.DASHSCOPE:
                if self.dashscope is None:
                    raise AssertionError("DashScope client missing after availability check")
                # Save intent first: even a crash between acceptance and task-ID
                # persistence must not silently cause a second paid submission.
                state = self._save(
                    candidate.page_id,
                    state.model_copy(update={"submission_uncertain": True}),
                )
                try:
                    task_id, model = self.dashscope.submit_with_fallback(candidate.audio_url)
                except ProviderError as exc:
                    if exc.failure.code != "ambiguous_submission":
                        state = self._save(
                            candidate.page_id,
                            state.model_copy(update={"submission_uncertain": False}),
                        )
                    if self._is_sensitive_asr_failure(exc) or state.submission_uncertain:
                        raise
                    last_error = exc
                    continue
                state = self._save(
                    candidate.page_id,
                    state.model_copy(
                        update={
                            "record": state.record.transition(PipelineState.ASR_SUBMITTED),
                            "provider": "dashscope",
                            "provider_task_id": task_id,
                            "provider_model": model,
                            "submission_uncertain": False,
                        }
                    ),
                )
                return self._advance_asr(candidate, state)
            try:
                transcript = (
                    self._siliconflow(candidate)
                    if provider is AsrProvider.SILICONFLOW
                    else self._local_whisper(candidate)
                )
            except ProviderError as exc:
                if self._is_sensitive_asr_failure(exc):
                    raise
                last_error = exc
                continue
            record = state.record.transition(PipelineState.TRANSCRIBED)
            return (
                state.model_copy(
                    update={
                        "record": record,
                        "provider": transcript.provider,
                        "provider_task_id": transcript.provider_task_id,
                        "transcript": transcript,
                        "summary": None,
                    }
                ),
                False,
            )
        if last_error is not None:
            raise last_error
        return state, True

    def _fail(
        self,
        candidate: EpisodeCandidate,
        state: EpisodeAIState,
        error: ProviderError,
    ) -> ProcessingOutcome:
        target = (
            PipelineState.FAILED_RETRYABLE
            if error.failure.retryable and state.record.attempts < MAX_RETRY_ATTEMPTS
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
        self._checkpoint = state
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
                if (
                    self.dashscope is None
                    and self.siliconflow is None
                    and self.local_whisper is None
                ):
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
                published = self._publish(candidate, state)
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
            return self._fail(candidate, self._checkpoint or state, exc)
        except AsrDeferredError as exc:
            return ProcessingOutcome(candidate.eid, "budget_paused", state.record.state, str(exc))
        except (AudioPreparationError, OSError, httpx.HTTPError, ValueError) as exc:
            return self._fail(
                candidate,
                self._checkpoint or state,
                _failure(
                    ProviderErrorCategory.UNAVAILABLE,
                    f"Audio preparation or response validation failed: {type(exc).__name__}: {exc}"
                    if isinstance(exc, AudioPreparationError)
                    else f"Audio preparation or response validation failed: {type(exc).__name__}",
                ),
            )

    def process_asr_only(
        self,
        candidate: EpisodeCandidate,
        page: Mapping[str, Any],
        *,
        retry_failed: bool = False,
    ) -> ProcessingOutcome:
        """Advance only ASR checkpoints and stop permanently at TRANSCRIBED."""
        state = self.state_store.load(page, candidate.eid)
        self._checkpoint = state
        asr_states = {
            PipelineState.DISCOVERED,
            PipelineState.ASR_SUBMITTED,
            PipelineState.ASR_RUNNING,
        }
        if state.record.state is PipelineState.FAILED_RETRYABLE:
            if not retry_failed:
                return ProcessingOutcome(candidate.eid, "waiting_retry", state.record.state)
            # A summary/publish failure belongs to the enrichment queue.  Do not
            # resume it here, otherwise an ASR-only pass could consume its retry.
            if state.record.resume_state not in asr_states:
                return ProcessingOutcome(candidate.eid, "skipped", state.record.state)
            state = state.model_copy(update={"record": state.record.resume()})
            state = self._save(candidate.page_id, state)
        if state.record.state not in asr_states:
            return ProcessingOutcome(candidate.eid, "skipped", state.record.state)
        if self.dashscope is None and self.siliconflow is None and self.local_whisper is None:
            return ProcessingOutcome(candidate.eid, "paused", state.record.state)
        try:
            state, pending = self._advance_asr(candidate, state)
            state = self._save(candidate.page_id, state)
            return ProcessingOutcome(
                candidate.eid,
                "pending" if pending else "transcribed",
                state.record.state,
                detail=state.provider or "unknown",
            )
        except ProviderError as exc:
            return self._fail(candidate, self._checkpoint or state, exc)
        except AsrDeferredError as exc:
            return ProcessingOutcome(candidate.eid, "budget_paused", state.record.state, str(exc))
        except (AudioPreparationError, OSError, httpx.HTTPError, ValueError) as exc:
            return self._fail(
                candidate,
                self._checkpoint or state,
                _failure(
                    ProviderErrorCategory.UNAVAILABLE,
                    f"Audio preparation or response validation failed: {type(exc).__name__}: {exc}"
                    if isinstance(exc, AudioPreparationError)
                    else f"Audio preparation or response validation failed: {type(exc).__name__}",
                ),
            )


def build_summary_client(
    *,
    dashscope_api_key: SecretStr | None,
    dashscope_model: str,
    siliconflow_api_key: SecretStr | None,
    siliconflow_models: tuple[str, ...],
    local_qwen_summary: bool,
    local_summary_progress: Callable[[str], None] | None = None,
) -> StructuredSummaryClient | None:
    """Build the shared DashScope -> SiliconFlow -> optional local route."""
    dashscope_summary = (
        DashScopeSummaryClient(dashscope_api_key, model=dashscope_model)
        if dashscope_api_key is not None
        else None
    )
    siliconflow_summary = (
        SiliconFlowSummaryClient(siliconflow_api_key, models=siliconflow_models)
        if siliconflow_api_key is not None
        else None
    )
    local_summary = (
        LocalQwenSummaryClient(progress=local_summary_progress) if local_qwen_summary else None
    )
    return chain_summary_clients(
        dashscope_summary,
        siliconflow_summary,
        local_summary,
    )


def build_provider_clients(
    *,
    dashscope_api_key: SecretStr | None = None,
    dashscope_model: str = "paraformer-v1",
    dashscope_models: tuple[str, ...] | None = None,
    provider_poll_attempts: int = 60,
    dashscope_summary_api_key: SecretStr | None = None,
    dashscope_summary_model: str = "qwen-flash",
    siliconflow_asr_api_key: SecretStr | None,
    siliconflow_summary_api_key: SecretStr | None,
    siliconflow_asr_models: tuple[str, ...],
    siliconflow_summary_models: tuple[str, ...],
    local_whisper_model: str | None = None,
    local_qwen_summary: bool = True,
    local_summary_progress: Callable[[str], None] | None = None,
) -> tuple[
    DashScopeParaformerClient | None,
    SiliconFlowClient | None,
    LocalWhisperClient | None,
    StructuredSummaryClient | None,
]:
    """Construct only explicitly configured user-owned providers."""
    summary_client = build_summary_client(
        dashscope_api_key=dashscope_summary_api_key,
        dashscope_model=dashscope_summary_model,
        siliconflow_api_key=siliconflow_summary_api_key,
        siliconflow_models=siliconflow_summary_models,
        local_qwen_summary=local_qwen_summary,
        local_summary_progress=local_summary_progress,
    )
    return (
        DashScopeParaformerClient(
            dashscope_api_key,
            model=dashscope_model,
            models=dashscope_models,
            poll_attempts=provider_poll_attempts,
        )
        if dashscope_api_key is not None
        else None,
        SiliconFlowClient(siliconflow_asr_api_key, models=siliconflow_asr_models)
        if siliconflow_asr_api_key is not None
        else None,
        LocalWhisperClient(local_whisper_model) if local_whisper_model is not None else None,
        summary_client,
    )
