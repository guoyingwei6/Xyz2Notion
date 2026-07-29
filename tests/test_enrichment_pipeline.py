from dataclasses import replace

import pytest

from xyz2notion.enrichment.dashscope import CompletionUsage
from xyz2notion.enrichment.pipeline import (
    SummaryPolicy,
    TranscriptEnricher,
    validate_payload,
)
from xyz2notion.enrichment.schema import EnrichmentPayload
from xyz2notion.models import (
    Chapter,
    MindmapNode,
    ProviderError,
    TranscriptResult,
    TranscriptSegment,
)


def enrichment_payload(summary: str = "摘要") -> EnrichmentPayload:
    return EnrichmentPayload(
        summary=summary,
        chapters=(Chapter(start_ms=0, title="开场"),),
        highlights=("观点",),
        quotes=("金句",),
        terms=("术语",),
        people=("人物",),
        questions=("问题",),
        mindmap=MindmapNode(node_id="root", title="主题"),
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate_structured(
        self,
        _model_type: object,
        *,
        model: str,
        system: str,
        user: str,
        max_output_tokens: int,
        validator: object,
    ) -> tuple[EnrichmentPayload, CompletionUsage]:
        assert model == "qwen-flash"
        assert system
        assert max_output_tokens == 8192
        self.calls.append(user)
        result = enrichment_payload(f"摘要-{len(self.calls)}")
        assert validator(result)  # type: ignore[operator]
        return result, CompletionUsage(100, 20)


def short_transcript() -> TranscriptResult:
    return TranscriptResult(
        provider="siliconflow",
        provider_task_id="task",
        model="model",
        duration_ms=60_000,
        text="全文",
        segments=(TranscriptSegment(start_ms=0, end_ms=60_000, text="播客文字稿"),),
    )


def test_single_chunk_generates_complete_result_and_cost() -> None:
    client = FakeClient()
    result = TranscriptEnricher(client).summarize(short_transcript())  # type: ignore[arg-type]
    assert len(client.calls) == 1
    assert result.summary == "摘要-1"
    assert result.quotes == ("金句",)
    assert result.terms == ("术语",)
    assert result.people == ("人物",)
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.estimated_cost_cny == 0.000045
    assert result.prompt_version == "summary-v1"


def test_long_transcript_uses_map_reduce_and_accumulates_usage() -> None:
    client = FakeClient()
    transcript = TranscriptResult(
        provider="tingwu_cookie",
        provider_task_id="task",
        model="model",
        duration_ms=180_000,
        text="全文",
        segments=(
            TranscriptSegment(start_ms=0, end_ms=60_000, text="甲" * 20),
            TranscriptSegment(start_ms=60_000, end_ms=120_000, text="乙" * 20),
            TranscriptSegment(start_ms=120_000, end_ms=180_000, text="丙" * 20),
        ),
    )
    policy = replace(SummaryPolicy(), chunk_tokens=30, chunk_minutes=5)
    result = TranscriptEnricher(client, policy=policy).summarize(transcript)  # type: ignore[arg-type]
    assert len(client.calls) == 4
    assert "分段摘要" in client.calls[-1]
    assert result.summary == "摘要-4"
    assert result.input_tokens == 400
    assert result.output_tokens == 80


def test_payload_timeline_and_mindmap_ids_are_validated() -> None:
    valid = enrichment_payload()
    assert validate_payload(valid, 1_000)

    unsorted = valid.model_copy(
        update={
            "chapters": (
                Chapter(start_ms=10, title="二"),
                Chapter(start_ms=0, title="一"),
            )
        }
    )
    assert not validate_payload(unsorted, 1_000)

    out_of_range = valid.model_copy(update={"chapters": (Chapter(start_ms=1_001, title="远"),)})
    assert not validate_payload(out_of_range, 1_000)

    duplicate = valid.model_copy(
        update={
            "mindmap": MindmapNode(
                node_id="same",
                title="根",
                children=(MindmapNode(node_id="same", title="子"),),
            )
        }
    )
    assert not validate_payload(duplicate, 1_000)


def test_invalid_prompt_version_and_empty_transcript_fail_without_asr() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        TranscriptEnricher(
            FakeClient(),  # type: ignore[arg-type]
            policy=replace(SummaryPolicy(), prompt_version="unknown"),
        )

    empty = short_transcript().model_copy(
        update={
            "text": "[音乐]",
            "segments": (TranscriptSegment(start_ms=0, end_ms=60_000, text="[音乐]"),),
        }
    )
    with pytest.raises(ProviderError) as caught:
        TranscriptEnricher(FakeClient()).summarize(empty)  # type: ignore[arg-type]
    assert "no readable content" in str(caught.value)
