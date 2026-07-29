from xml.etree import ElementTree

from xyz2notion.models import MindmapNode
from xyz2notion.notion.mindmap import render_mindmap_svg


def test_mindmap_svg_is_deterministic_accessible_and_escaped() -> None:
    root = MindmapNode(
        node_id="root",
        title="主题 & 方法",
        children=(
            MindmapNode(
                node_id="one",
                title="<观点一>",
                children=(MindmapNode(node_id="deep", title="细节"),),
            ),
            MindmapNode(node_id="two", title="观点二"),
        ),
    )
    first = render_mindmap_svg(root)
    second = render_mindmap_svg(root)
    assert first == second
    assert first.startswith(b"<svg")
    assert b"Podcast mind map" in first
    assert b"&amp;" in first
    assert b"&lt;" in first
    assert first.count(b"<rect") == 5  # background plus four nodes
    ElementTree.fromstring(first)  # noqa: S314 - parses only our generated fixture
