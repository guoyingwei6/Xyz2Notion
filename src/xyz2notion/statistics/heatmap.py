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
MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_FONT_3X5 = {
    "A": ("010", "101", "111", "101", "101"),
    "D": ("110", "101", "101", "101", "110"),
    "F": ("111", "100", "110", "100", "100"),
    "J": ("111", "001", "001", "101", "010"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "S": ("011", "100", "010", "001", "110"),
    "W": ("101", "101", "111", "111", "101"),
    "a": ("000", "011", "101", "101", "011"),
    "b": ("100", "110", "101", "101", "110"),
    "c": ("000", "011", "100", "100", "011"),
    "d": ("001", "011", "101", "101", "011"),
    "e": ("000", "010", "101", "110", "011"),
    "g": ("000", "011", "101", "011", "110"),
    "i": ("010", "000", "010", "010", "010"),
    "l": ("100", "100", "100", "100", "011"),
    "n": ("000", "110", "101", "101", "101"),
    "o": ("000", "010", "101", "101", "010"),
    "p": ("000", "110", "101", "110", "100"),
    "r": ("000", "101", "110", "100", "100"),
    "t": ("010", "111", "010", "010", "001"),
    "u": ("000", "101", "101", "101", "011"),
    "v": ("000", "101", "101", "101", "010"),
    "y": ("000", "101", "011", "001", "110"),
}


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
    left = 38
    top = 32
    width = left + weeks * (cell_size + gap)
    height = top + 7 * (cell_size + gap)
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
        x = left + week * (cell_size + gap)
        y = top + weekday * (cell_size + gap)
        label = escape(f"{day.isoformat()}: {seconds} seconds")
        rects.append(
            f'<rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" '
            f'rx="2" fill="{COLORS[level]}" data-date="{day.isoformat()}" '
            f'data-seconds="{seconds}" data-level="{level}">'
            f"<title>{label}</title></rect>"
        )
    month_labels = []
    for month, label in enumerate(MONTH_LABELS, start=1):
        month_start = date(year, month, 1)
        week = (month_start - grid_start).days // 7
        x = left + week * (cell_size + gap)
        month_labels.append(
            f'<text x="{x}" y="25" font-family="sans-serif" font-size="10" '
            f'fill="#57606a">{label}</text>'
        )
    weekday_labels = "".join(
        f'<text x="4" y="{top + row * (cell_size + gap) + cell_size}" '
        f'font-family="sans-serif" font-size="9" fill="#57606a">{label}</text>'
        for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri"))
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{year} listening heatmap">'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<text x="4" y="16" font-family="sans-serif" font-size="12" '
        f'fill="#24292f">{year} 播客收听</text>'
        + "".join(month_labels)
        + weekday_labels
        + "".join(rects)
        + "</svg>"
    )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum)
    return (
        struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum & 0xFFFFFFFF)
    )


def _draw_text(
    pixels: list[list[tuple[int, int, int]]],
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
) -> None:
    """Draw compact labels with a deterministic embedded 3x5 bitmap font."""
    cursor = x
    for character in text:
        glyph = _FONT_3X5.get(character)
        if glyph is None:
            cursor += 4
            continue
        for row, pattern in enumerate(glyph):
            for column, bit in enumerate(pattern):
                if bit == "1":
                    pixels[y + row][cursor + column] = color
        cursor += 4


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
    left = 16
    top = 11
    width = left + weeks * (cell_size + gap) - gap + margin
    height = top + 7 * (cell_size + gap) - gap + margin
    background = (255, 255, 255)
    pixels = [[background for _x in range(width)] for _y in range(height)]
    label_color = (87, 96, 106)
    for month, label in enumerate(MONTH_LABELS, start=1):
        month_start = date(year, month, 1)
        week = (month_start - grid_start).days // 7
        _draw_text(
            pixels,
            left + week * (cell_size + gap),
            2,
            label,
            label_color,
        )
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        _draw_text(
            pixels,
            1,
            top + row * (cell_size + gap) + 1,
            label,
            label_color,
        )
    for day_offset in range(weeks * 7):
        day = grid_start + timedelta(days=day_offset)
        if day.year != year:
            continue
        value = values.get(day)
        level = value.level if value else 0
        x = left + (day_offset // 7) * (cell_size + gap)
        y = top + (day_offset % 7) * (cell_size + gap)
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
