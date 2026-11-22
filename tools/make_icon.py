"""Draw the ShelfLife logo (the three books on a shelf from the top bar) as shelflife/web/static/shelflife.ico.

Run from the project root:  python tools/make_icon.py
Plain Python, no image library: it samples each pixel several times to smooth the edges and writes
PNG images inside the .ico (16, 32, 64, 128 and 256 pixels). Used for the app window, the shortcut and
the browser tab.
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "shelflife" / "web" / "static" / "shelflife.ico"
SIZE = 256
SAMPLES = 3  # per pixel side
BACKGROUND = (20, 23, 28)
# The logo's 24x24 drawing: x, y, width, height, corner radius, colour, rotation in degrees about (cx, cy)
SHAPES = [
    (3, 7, 4.5, 12, 1, (63, 207, 155), 0, None),
    (8.5, 4, 4.5, 15, 1, (42, 157, 116), 0, None),
    (14.6, 6.4, 4.5, 12.6, 1, (127, 227, 189), 12, (16.85, 19)),
    (1.5, 19.5, 21, 2, 1, (201, 206, 214), 0, None),
]
SCALE = 8.6  # pixels per drawing unit
OFFSET = (SIZE - 24 * SCALE) / 2


def inside(px: float, py: float, x: float, y: float, w: float, h: float, r: float) -> bool:
    """Is the point inside the rounded rectangle?"""
    if not (x <= px <= x + w and y <= py <= y + h):
        return False
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def colour_at(u: float, v: float):
    """The colour (r, g, b, a) at a point of the 256x256 icon, or None for transparent."""
    if not inside(u, v, 0, 0, SIZE, SIZE, SIZE * 0.22):
        return None
    px, py = (u - OFFSET) / SCALE, (v - OFFSET) / SCALE
    found = BACKGROUND
    for x, y, w, h, r, colour, angle, centre in SHAPES:
        qx, qy = px, py
        if angle:
            a = math.radians(-angle)
            dx, dy = px - centre[0], py - centre[1]
            qx, qy = centre[0] + dx * math.cos(a) - dy * math.sin(a), centre[1] + dx * math.sin(a) + dy * math.cos(a)
        if inside(qx, qy, x, y, w, h, r):
            found = colour
    return found


def render() -> list[list[tuple[float, float, float, float]]]:
    """The icon as rows of premultiplied-free (r, g, b, a) averages."""
    rows = []
    for j in range(SIZE):
        row = []
        for i in range(SIZE):
            r = g = b = a = 0.0
            for sj in range(SAMPLES):
                for si in range(SAMPLES):
                    c = colour_at(i + (si + 0.5) / SAMPLES, j + (sj + 0.5) / SAMPLES)
                    if c:
                        r, g, b, a = r + c[0], g + c[1], b + c[2], a + 1
            n = SAMPLES * SAMPLES
            row.append((r / a, g / a, b / a, 255 * a / n) if a else (0, 0, 0, 0))
        rows.append(row)
    return rows


def shrink(rows, size: int):
    """The image at `size` pixels, averaging blocks (colour weighted by opacity)."""
    k = len(rows) // size
    out = []
    for j in range(size):
        row = []
        for i in range(size):
            r = g = b = a = w = 0.0
            for dj in range(k):
                for di in range(k):
                    pr, pg, pb, pa = rows[j * k + dj][i * k + di]
                    r, g, b, a, w = r + pr * pa, g + pg * pa, b + pb * pa, a + pa, w + 1
            row.append((r / a, g / a, b / a, a / w) if a else (0, 0, 0, 0))
        out.append(row)
    return out


def png(rows) -> bytes:
    size = len(rows)
    raw = b"".join(b"\x00" + bytes(round(c) for px in row for c in px) for row in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> int:
    full = render()
    images = [(s, png(full if s == SIZE else shrink(full, s))) for s in (16, 32, 64, 128, 256)]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    body = b""
    for s, data in images:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset + len(body))
        body += data
    OUT.write_bytes(out + body)
    print(f"Wrote {OUT} ({len(out) + len(body)} bytes, sizes {[s for s, _ in images]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
