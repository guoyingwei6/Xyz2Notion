"""Strict structured-output contracts for Qwen enrichment."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from xyz2notion.models import Chapter, MindmapNode


class EnrichmentPayload(BaseModel):
    """The exact JSON object required from the summary model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    chapters: tuple[Chapter, ...] = ()
    highlights: tuple[str, ...] = ()
    quotes: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    mindmap: MindmapNode
