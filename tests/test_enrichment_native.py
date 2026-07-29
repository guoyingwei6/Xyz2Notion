from xyz2notion.asr.tingwu import TingwuEnrichment
from xyz2notion.enrichment.native import normalize_tingwu_enrichment
from xyz2notion.models import Chapter


def test_complete_tingwu_native_result_needs_no_extra_model_call() -> None:
    native = TingwuEnrichment(
        summary="听悟摘要",
        chapters=(Chapter(start_ms=0, title="开场"),),
        questions=("问题",),
        mindmap={
            "content": "主题",
            "children": [{"content": "观点", "children": []}],
        },
    )
    result = normalize_tingwu_enrichment(native)
    assert result is not None
    assert result.summary == "听悟摘要"
    assert result.prompt_version == "tingwu-native"
    assert result.estimated_cost_cny == 0
    assert result.mindmap.children[0].title == "观点"
    assert result.mindmap.node_id != result.mindmap.children[0].node_id


def test_incomplete_or_malformed_native_result_requests_qwen_fallback() -> None:
    assert normalize_tingwu_enrichment(TingwuEnrichment(summary="摘要")) is None
    assert (
        normalize_tingwu_enrichment(TingwuEnrichment(summary="摘要", mindmap={"children": []}))
        is None
    )
    assert (
        normalize_tingwu_enrichment(
            TingwuEnrichment(
                summary="摘要",
                mindmap={"content": "主题", "children": "not-a-list"},
            )
        )
        is None
    )
