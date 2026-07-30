"""Long-transcript map/reduce enrichment without repeating ASR."""

from __future__ import annotations

import json
from dataclasses import dataclass

from xyz2notion.enrichment.prompts import (
    CHUNK_PROMPT,
    FULL_PROMPT,
    PROMPT_VERSION,
    SYNTHESIS_PROMPT,
    SYSTEM_PROMPT,
)
from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.enrichment.siliconflow import CompletionUsage, SiliconFlowSummaryClient
from xyz2notion.enrichment.text import chunk_transcript
from xyz2notion.models import (
    MindmapNode,
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    SummaryResult,
    TranscriptResult,
)


@dataclass(frozen=True)
class SummaryPolicy:
    """Chunking policy used for one free summary run."""

    prompt_version: str = PROMPT_VERSION
    chunk_tokens: int = 24_000
    chunk_minutes: int = 30
    max_output_tokens: int = 8_192


def _node_ids(root: MindmapNode) -> tuple[str, ...]:
    return (
        root.node_id,
        *(identifier for child in root.children for identifier in _node_ids(child)),
    )


def validate_payload(payload: EnrichmentPayload, duration_ms: int) -> bool:
    """Check cross-field invariants that JSON Schema alone cannot express."""
    starts = [chapter.start_ms for chapter in payload.chapters]
    identifiers = _node_ids(payload.mindmap)
    return (
        starts == sorted(starts)
        and all(start <= duration_ms for start in starts)
        and len(identifiers) == len(set(identifiers))
    )


def normalize_payload(payload: EnrichmentPayload, duration_ms: int) -> EnrichmentPayload:
    """Deterministically repair harmless model ordering, range, and ID mistakes."""
    chapters = tuple(
        sorted(
            (
                chapter.model_copy(update={"start_ms": min(chapter.start_ms, duration_ms)})
                for chapter in payload.chapters
            ),
            key=lambda chapter: (chapter.start_ms, chapter.title),
        )
    )
    seen: set[str] = set()

    def normalize_node(node: MindmapNode, path: tuple[int, ...]) -> MindmapNode:
        identifier = node.node_id
        if identifier in seen:
            suffix = "-".join(str(index) for index in path) or "root"
            identifier = f"{identifier}-{suffix}"
            while identifier in seen:
                identifier = f"{identifier}-next"
        seen.add(identifier)
        children = tuple(
            normalize_node(child, (*path, index))
            for index, child in enumerate(node.children, start=1)
        )
        return node.model_copy(
            update={
                "node_id": identifier,
                "children": children,
            }
        )

    return payload.model_copy(
        update={
            "chapters": chapters,
            "mindmap": normalize_node(payload.mindmap, ()),
        }
    )


def _provider_error(message: str) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="siliconflow_summary",
            category=ProviderErrorCategory.INVALID_INPUT,
            message=message,
        )
    )


class TranscriptEnricher:
    """Generate a unified SummaryResult from any provider's transcript."""

    def __init__(
        self,
        client: SiliconFlowSummaryClient,
        *,
        policy: SummaryPolicy | None = None,
    ) -> None:
        self.client = client
        self.policy = policy or SummaryPolicy()
        if self.policy.prompt_version != PROMPT_VERSION:
            raise ValueError(f"unsupported summary prompt version: {self.policy.prompt_version}")

    def _generate(
        self,
        user: str,
        duration_ms: int,
    ) -> tuple[EnrichmentPayload, CompletionUsage]:
        payload, usage = self.client.generate_structured(
            EnrichmentPayload,
            system=SYSTEM_PROMPT,
            user=user,
            max_output_tokens=self.policy.max_output_tokens,
            validator=None,
        )
        normalized = normalize_payload(payload, duration_ms)
        if not validate_payload(normalized, duration_ms):
            raise ProviderError(
                ProviderFailure(
                    provider="siliconflow_summary",
                    category=ProviderErrorCategory.SCHEMA_CHANGED,
                    message="Local enrichment normalization did not satisfy constraints",
                )
            )
        return normalized, usage

    def summarize(self, transcript: TranscriptResult) -> SummaryResult:
        """Summarize once from persisted transcript data; this method has no ASR access."""
        chunks = chunk_transcript(
            transcript,
            max_tokens=self.policy.chunk_tokens,
            max_duration_ms=self.policy.chunk_minutes * 60 * 1000,
        )
        if not chunks:
            raise _provider_error("Transcript contains no readable content")
        schema = json.dumps(
            EnrichmentPayload.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        usage = CompletionUsage()
        if len(chunks) == 1:
            payload, call_usage = self._generate(
                FULL_PROMPT.format(
                    duration_ms=transcript.duration_ms,
                    schema=schema,
                    transcript=chunks[0].text,
                ),
                transcript.duration_ms,
            )
            usage += call_usage
        else:
            digests: list[dict[str, object]] = []
            for chunk in chunks:
                digest, call_usage = self._generate(
                    CHUNK_PROMPT.format(
                        index=chunk.index,
                        count=len(chunks),
                        start_ms=chunk.start_ms,
                        end_ms=chunk.end_ms,
                        schema=schema,
                        transcript=chunk.text,
                    ),
                    transcript.duration_ms,
                )
                usage += call_usage
                digests.append(digest.model_dump(mode="json"))
            payload, call_usage = self._generate(
                SYNTHESIS_PROMPT.format(
                    duration_ms=transcript.duration_ms,
                    schema=schema,
                    digests=json.dumps(digests, ensure_ascii=False),
                ),
                transcript.duration_ms,
            )
            usage += call_usage
        return SummaryResult(
            summary=payload.summary,
            chapters=payload.chapters,
            highlights=payload.highlights,
            quotes=payload.quotes,
            terms=payload.terms,
            people=payload.people,
            questions=payload.questions,
            mindmap=payload.mindmap,
            prompt_version=self.policy.prompt_version,
            model=self.client.active_model or self.client.models[0],
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_cny=0,
        )
