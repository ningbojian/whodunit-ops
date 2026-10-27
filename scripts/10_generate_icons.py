"""生成 Web 端 PWA 图标，只用标准库。

用法：
    python scripts/10_generate_icons.py

输出到 app/web/static/：
    icon-192.png / icon-512.png / apple-touch-icon.png / favicon.png
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "web" / "static"

CYAN = (34, 211, 238)
VIOLET = (139, 92, 246)
PINK = (244, 114, 182)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge0 == edge1:
        return 0.0
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _background(u: float, v: float) -> tuple[int, int, int]:
    # 深墨底 + 左上青色、右下紫色光晕。
    base = (5, 7, 15)
    cyan_glow = max(0.0, 1.0 - math.hypot(u - 0.18, v - 0.12) / 0.85)
    violet_glow = max(0.0, 1.0 - math.hypot(u - 0.86, v - 0.92) / 0.9)
    color = _mix(base, (12, 20, 40), 0.55)
    color = _mix(color, CYAN, 0.22 * cyan_glow ** 2)
    color = _mix(color, VIOLET, 0.24 * violet_glow ** 2)
    return color


def render(size: int) -> bytes:
    rows: list[bytes] = []
    cx = cy = size / 2.0
    radius = size * 0.30
    thickness = size * 0.052
    for y in range(size):
        row = bytearray()
        for x in range(size):
            u = (x + 0.5) / size
            v = (y + 0.5) / size
            color = _background(u, v)

            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            dist = math.hypot(dx, dy)
            angle = math.atan2(dy, dx)

            # 外圈：青色到紫色的角度渐变，底部留一个缺口（场控计时环）。
            gap = abs(angle - math.pi / 2.0) < 0.42
            ring_alpha = 0.0
            if not gap:
                ring_alpha = _smoothstep(thickness, 0.0, abs(dist - radius))
            if ring_alpha > 0:
                t = (math.sin(angle) + 1.0) / 2.0
                ring = _mix(CYAN, VIOLET, 0.25 + 0.65 * t)
                color = _mix(color, ring, ring_alpha * 0.95)

            # 内圈细线，增加层次。
            inner = radius * 0.66
            inner_alpha = _smoothstep(size * 0.008, 0.0, abs(dist - inner))
            if inner_alpha > 0:
                color = _mix(color, PINK, inner_alpha * 0.35)

            # 中心圆点。
            dot = _smoothstep(size * 0.085, 0.0, dist)
            if dot > 0:
                color = _mix(color, (232, 240, 255), dot * 0.92)

            row.extend(color)
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    compressed = zlib.compress(raw, 9)
    return _png(size, size, compressed)


def _png(width: int, height: int, compressed: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in (
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
        ("favicon.png", 64),
    ):
        data = render(size)
        (OUT_DIR / name).write_bytes(data)
        print(f"{name}: {size}x{size}, {len(data)} bytes")


if __name__ == "__main__":
    main()
