"""Draw the app icon (blue tile, white database cylinder) and write installer/app.ico.

Pure Python (zlib only), so building needs no image library. The .ico holds PNG images at 16-256 px.
Run: python installer/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4   # supersampling per axis

TOP, BOTTOM = (59, 130, 246), (29, 78, 216)       # tile gradient
WHITE, LID, BAND = (255, 255, 255), (191, 219, 254), (147, 197, 253)
CX, RX, RY = 0.5, 0.25, 0.075                     # cylinder centre x, radii
Y_TOP, Y_BOTTOM = 0.29, 0.69


def _in_tile(x: float, y: float) -> bool:
    m, r = 0.04, 0.2
    if not (m <= x <= 1 - m and m <= y <= 1 - m):
        return False
    cx = min(max(x, m + r), 1 - m - r)
    cy = min(max(y, m + r), 1 - m - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _ellipse(x: float, y: float, cy: float) -> float:
    return ((x - CX) / RX) ** 2 + ((y - cy) / RY) ** 2


def _sample(x: float, y: float):
    if not _in_tile(x, y):
        return None
    t = y
    color = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
    in_body = abs(x - CX) <= RX and Y_TOP <= y <= Y_BOTTOM
    if in_body or _ellipse(x, y, Y_BOTTOM) <= 1:
        color = WHITE
        for band in (Y_TOP + (Y_BOTTOM - Y_TOP) / 3, Y_TOP + 2 * (Y_BOTTOM - Y_TOP) / 3):
            if y >= band and abs(_ellipse(x, y, band) - 1) < 0.28:
                color = BAND
    if _ellipse(x, y, Y_TOP) <= 1:
        color = LID
    return color


def render(size: int) -> bytes:
    rows = []
    n = SS * SS
    for py in range(size):
        row = bytearray([0])   # PNG filter: none
        for px in range(size):
            acc = [0, 0, 0]
            hits = 0
            for sy in range(SS):
                for sx in range(SS):
                    c = _sample((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if c is not None:
                        hits += 1
                        for i in range(3):
                            acc[i] += c[i]
            if hits:
                row += bytes([round(acc[0] / hits), round(acc[1] / hits), round(acc[2] / hits), round(255 * hits / n)])
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return _png(size, b"".join(rows))


def _png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def write_ico(path: Path) -> None:
    images = [(s, render(s)) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out += b"".join(data for _, data in images)
    path.write_bytes(out)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "app.ico"
    write_ico(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
