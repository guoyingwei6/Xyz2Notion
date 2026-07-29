"""Deterministic, self-contained SVG rendering for a summary mind map."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from xyz2notion.models import MindmapNode


@dataclass(frozen=True)
class PositionedNode:
    node: MindmapNode
    depth: int
    row: int
    parent_row: int | None
    parent_depth: int | None


def _position(
    node: MindmapNode,
    *,
    depth: int = 0,
    parent_row: int | None = None,
    parent_depth: int | None = None,
    output: list[PositionedNode] | None = None,
) -> tuple[PositionedNode, ...]:
    positioned = output if output is not None else []
    row = len(positioned)
    positioned.append(PositionedNode(node, depth, row, parent_row, parent_depth))
    for child in node.children:
        _position(
            child,
            depth=depth + 1,
            parent_row=row,
            parent_depth=depth,
            output=positioned,
        )
    return tuple(positioned)


def render_mindmap_svg(root: MindmapNode) -> bytes:
    """Render an accessible tree diagram without an external image service."""
    nodes = _position(root)
    max_depth = max(item.depth for item in nodes)
    width = max(640, 80 + (max_depth + 1) * 240)
    height = max(180, 60 + len(nodes) * 72)
    colors = ("#6d5dfc", "#00a6a6", "#e07a5f", "#3d9970")
    lines: list[str] = []
    boxes: list[str] = []
    for item in nodes:
        x = 40 + item.depth * 240
        y = 30 + item.row * 72
        if item.parent_row is not None and item.parent_depth is not None:
            parent_x = 40 + item.parent_depth * 240
            parent_y = 30 + item.parent_row * 72
            lines.append(
                f'<path d="M {parent_x + 190} {parent_y + 24} '
                f'C {parent_x + 215} {parent_y + 24}, {x - 25} {y + 24}, {x} {y + 24}" '
                'fill="none" stroke="#a7a7b3" stroke-width="2"/>'
            )
        color = colors[item.depth % len(colors)]
        title = escape(item.node.title[:28])
        boxes.append(
            f'<g><rect x="{x}" y="{y}" width="190" height="48" rx="12" '
            f'fill="{color}" opacity="0.94"/>'
            f'<text x="{x + 12}" y="{y + 29}" font-family="sans-serif" '
            f'font-size="14" fill="#ffffff">{title}</text>'
            f"<title>{escape(item.node.title)}</title></g>"
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Podcast mind map">'
        '<rect width="100%" height="100%" fill="#fbfbfe"/>'
        + "".join(lines)
        + "".join(boxes)
        + "</svg>"
    )
    return svg.encode("utf-8")
