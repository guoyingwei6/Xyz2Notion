"""GitHub-style SVG and dependency-free PNG heatmap rendering."""

from __future__ import annotations

import struct
import zlib
from datetime import date, timedelta
from html import escape

from xyz2notion.statistics.calculator import DailyListening

COLORS = (
    "#ebedf0",
    "#9be9a8",
    "#40c463",
    "#30a14e",
    "#216e39",
)
RGB_COLORS = (
    (235, 237, 240),
    (155, 233, 168),
    (64, 196, 99),
    (48, 161, 78),
    (33, 110, 57),
)


def _year_grid(year: int) -> tuple[date, int]:
    first = date(year, 1, 1)
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
    last = date(year, 12, 31)
    weeks = ((last - grid_start).days // 7) + 1
    return grid_start, weeks


def render_heatmap_svg(
    year: int,
    daily: tuple[DailyListening, ...],
    *,
    cell_size: int = 11,
    gap: int = 3,
) -> str:
    """Render accessible deterministic SVG without external services."""
    grid_start, weeks = _year_grid(year)
    values = {value.day: value for value in daily if value.day.year == year}
    width = 42 + weeks * (cell_size + gap)
    height = 34 + 7 * (cell_size + gap)
    rects: list[str] = []
    for day_offset in range(weeks * 7):
        day = grid_start + timedelta(days=day_offset)
        if day.year != year:
            continue
        value = values.get(day)
        level = value.level if value else 0
        seconds = value.listening_seconds if value else 0
        week = day_offset // 7
        weekday = day_offset % 7
        x = 38 + week * (cell_size + gap)
        y = 28 + weekday * (cell_size + gap)
        label = escape(f"{day.isoformat()}: {seconds} seconds")
        rects.append(
            f'<rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" '
            f'rx="2" fill="{COLORS[level]}" data-date="{day.isoformat()}" '
            f'data-seconds="{seconds}" data-level="{level}">'
            f"<title>{label}</title></rect>"
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{year} listening heatmap">'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<text x="4" y="16" font-family="sans-serif" font-size="12" '
        f'fill="#24292f">{year} 播客收听</text>' + "".join(rects) + "</svg>"
    )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum)
    return (
        struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum & 0xFFFFFFFF)
    )


def render_heatmap_png(
    year: int,
    daily: tuple[DailyListening, ...],
    *,
    cell_size: int = 8,
    gap: int = 2,
    margin: int = 4,
) -> bytes:
    """Render an RGB PNG using only the Python standard library."""
    grid_start, weeks = _year_grid(year)
    values = {value.day: value for value in daily if value.day.year == year}
    width = margin * 2 + weeks * (cell_size + gap) - gap
    height = margin * 2 + 7 * (cell_size + gap) - gap
    background = (255, 255, 255)
    pixels = [[background for _x in range(width)] for _y in range(height)]
    for day_offset in range(weeks * 7):
        day = grid_start + timedelta(days=day_offset)
        if day.year != year:
            continue
        value = values.get(day)
        level = value.level if value else 0
        x = margin + (day_offset // 7) * (cell_size + gap)
        y = margin + (day_offset % 7) * (cell_size + gap)
        for pixel_y in range(y, y + cell_size):
            for pixel_x in range(x, x + cell_size):
                pixels[pixel_y][pixel_x] = RGB_COLORS[level]
    scanlines = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in pixels
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + _png_chunk(b"IEND", b"")
    )
