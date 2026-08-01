"""Persist resumable AI state as a private JSON file in the user's Notion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from pydantic import ConfigDict, Field

from xyz2notion.asr.audio import validate_public_audio_url
from xyz2notion.models import ContractModel, SummaryResult, TranscriptResult
from xyz2notion.notion.client import JsonObject, NotionAPIError, rich_text
from xyz2notion.state import PipelineRecord, PipelineState

MAX_STATE_BYTES = 20 * 1024 * 1024


class EpisodeAIState(ContractModel):
    """All private checkpoints needed to continue without repeating ASR.

    ``extra=ignore`` keeps snapshots written by the retired web adapter
    readable.  New snapshots never write those legacy fields.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = 1
    record: PipelineRecord
    provider: str | None = None
    provider_task_id: str | None = None
    source_task_id: str | None = None
    transcript: TranscriptResult | None = None
    summary: SummaryResult | None = None
    content_version: str | None = None
    state_revision: int = Field(default=0, ge=0)


class NotionStateAPI(Protocol):
    def upload_file(
        self,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> str: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...


_STATUS_NAMES = {
    PipelineState.DISCOVERED: "待处理",
    PipelineState.ASR_SUBMITTED: "排队中",
    PipelineState.ASR_RUNNING: "转写中",
    PipelineState.TRANSCRIBED: "已转写",
    PipelineState.ENRICHED: "已增强",
    PipelineState.PUBLISHED: "已发布",
    PipelineState.FAILED_RETRYABLE: "可重试失败",
    PipelineState.FAILED_FINAL: "最终失败",
}


def _file_url(page: Mapping[str, Any]) -> str | None:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return None
    state_property = properties.get("AI State File")
    if not isinstance(state_property, Mapping):
        return None
    files = state_property.get("files")
    if not isinstance(files, list) or not files:
        return None
    first = files[0]
    if not isinstance(first, Mapping):
        return None
    file_value = first.get("file")
    external = first.get("external")
    if isinstance(file_value, Mapping) and file_value.get("url"):
        return str(file_value["url"])
    if isinstance(external, Mapping) and external.get("url"):
        return str(external["url"])
    return None


class NotionEpisodeStateStore:
    """Read and atomically point an Episode row at its newest state snapshot."""

    def __init__(
        self,
        api: NotionStateAPI,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api = api
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=60, follow_redirects=False)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> NotionEpisodeStateStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def load(self, page: Mapping[str, Any], eid: str) -> EpisodeAIState:
        """Load the latest signed Notion file, or initialize a discovered record."""
        url = _file_url(page)
        if url is None:
            return EpisodeAIState(record=PipelineRecord(eid=eid))
        current = validate_public_audio_url(url)
        for _redirect in range(6):
            response = self._http.get(current, follow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise NotionAPIError("Notion state download redirect has no Location")
                current = validate_public_audio_url(str(response.url.join(location)))
                continue
            if response.is_error:
                raise NotionAPIError(
                    f"Notion state download failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                    retryable=response.status_code in {429, 500, 502, 503, 504, 529},
                )
            if len(response.content) > MAX_STATE_BYTES:
                raise NotionAPIError("Notion state file exceeds 20 MiB safety limit")
            try:
                state = EpisodeAIState.model_validate_json(response.content)
            except ValueError as exc:
                raise NotionAPIError("Notion AI state file is invalid") from exc
            if state.record.eid != eid:
                raise NotionAPIError("Notion AI state file belongs to a different Episode")
            return state
        raise NotionAPIError("Notion state download exceeded redirect limit")

    def save(self, page_id: str, state: EpisodeAIState) -> EpisodeAIState:
        """Upload a new immutable snapshot, then switch the Episode file property."""
        revised = state.model_copy(update={"state_revision": state.state_revision + 1})
        content = revised.model_dump_json().encode()
        if len(content) > MAX_STATE_BYTES:
            raise NotionAPIError("AI state snapshot exceeds 20 MiB safety limit")
        filename = f"xyz2notion-state-{revised.record.eid}-r{revised.state_revision:04d}.json"
        upload_id = self.api.upload_file(filename, "application/json", content)
        failure = revised.record.failure
        properties: JsonObject = {
            "AI State File": {
                "files": [
                    {
                        "name": filename,
                        "type": "file_upload",
                        "file_upload": {"id": upload_id},
                    }
                ]
            },
            "ASR Status": {"select": {"name": _STATUS_NAMES[revised.record.state]}},
            "ASR Provider": {"rich_text": rich_text(revised.provider or "")},
            "ASR Task ID": {"rich_text": rich_text(revised.provider_task_id or "")},
            "ASR Source Task ID": {"rich_text": rich_text(revised.source_task_id or "")},
            "Failure Reason": {
                "rich_text": rich_text(failure.message[:2000] if failure is not None else "")
            },
            "Content Version": {"rich_text": rich_text(revised.content_version or "")},
        }
        if revised.transcript is not None:
            properties["ASR Model"] = {"rich_text": rich_text(revised.transcript.model)}
            properties["ASR Quality"] = {
                "rich_text": rich_text(revised.transcript.timing_quality.value)
            }
            if revised.transcript.accuracy_hint is not None:
                properties["ASR Accuracy"] = {"number": revised.transcript.accuracy_hint}
        self.api.update_page(page_id, {"properties": properties})
        return revised
