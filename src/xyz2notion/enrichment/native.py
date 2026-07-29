"""Normalize Tingwu's native lab output into the shared summary contract."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from xyz2notion.asr.tingwu import TingwuEnrichment
from xyz2notion.models import MindmapNode, SummaryResult


def _mindmap_node(value: object, path: tuple[int, ...] = ()) -> MindmapNode:
    if not isinstance(value, Mapping):
        raise ValueError("Tingwu mind map node must be an object")
    title = value.get("content") or value.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Tingwu mind map node must have a title")
    raw_children = value.get("children", [])
    if not isinstance(raw_children, Sequence) or isinstance(raw_children, str | bytes):
        raise ValueError("Tingwu mind map children must be a list")
    identity = f"{'.'.join(map(str, path))}:{title.strip()}".encode()
    node_id = hashlib.sha256(identity).hexdigest()[:16]
    return MindmapNode(
        node_id=node_id,
        title=title.strip(),
        children=tuple(
            _mindmap_node(child, (*path, index)) for index, child in enumerate(raw_children)
        ),
    )


def normalize_tingwu_enrichment(
    enrichment: TingwuEnrichment,
) -> SummaryResult | None:
    """Use zero-extra-cost native output when summary and mind map are complete."""
    if not enrichment.summary or not enrichment.mindmap:
        return None
    try:
        mindmap = _mindmap_node(enrichment.mindmap)
    except ValueError:
        return None
    return SummaryResult(
        summary=enrichment.summary,
        chapters=enrichment.chapters,
        questions=enrichment.questions,
        mindmap=mindmap,
        prompt_version="tingwu-native",
        model="tongyi-tingwu-web",
        input_tokens=0,
        output_tokens=0,
        estimated_cost_cny=0,
    )
