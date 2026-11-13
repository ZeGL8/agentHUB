# -*- coding: utf-8 -*-
"""生成 agentHUB 的 .ico 图标（纯标准库，不依赖 Pillow）。

设计：深色圆角底 + 三个连成一线的节点，寓意"多工具会话的汇合与交接"。
输出 16/24/32/48/64/128/256 七个尺寸的 ICO。
"""

import os
import struct
import sys

BG = (24, 26, 33)        # 深底
NODE = (108, 176, 255)   # 蓝：会话节点
LINK = (74, 110, 160)    # 连接线
ACCENT = (255, 176, 88)  # 橙：汇合点


def render(size: int) -> bytes:
    """渲染为 RGBA 像素（自下而上，BMP 行序）。"""
    s = float(size)
    px = [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]

    # 圆角矩形底（半径 = 22% 边）
    r = s * 0.22
    for y in range(size):
        for x in range(size):
            cx, cy = x + 0.5, y + 0.5
            # 到圆角矩形边界的距离
            dx = max(r - cx, cx - (s - r), 0.0)
            dy = max(r - cy, cy - (s - r), 0.0)
            if dx * dx + dy * dy <= r * r:
                px[y][x] = BG + (255,)

    def blend(x, y, color, alpha):
        if 0 <= x < size and 0 <= y < size:
            br, bg_, bb, ba = px[y][x]
            a = alpha
            px[y][x] = (
                int(color[0] * a + br * (1 - a)),
                int(color[1] * a + bg_ * (1 - a)),
                int(color[2] * a + bb * (1 - a)),
                255,
            )

    def dot(cx, cy, radius, color):
        rr = radius * radius
        for y in range(size):
            for x in range(size):
                d = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2
                if d <= rr:
                    # 边缘 1px 抗锯齿
                    a = 1.0 if d <= (radius - 0.7) ** 2 else max(0.0, 1.0 - (d ** 0.5 - (radius - 0.7)))
                    blend(x, y, color, min(1.0, a))
                elif d <= (radius + 1.0) ** 2:
                    a = max(0.0, 1.0 - ((d ** 0.5) - radius))
                    if a > 0:
                        blend(x, y, color, a * 0.85)

    def line(x1, y1, x2, y2, width, color):
        steps = int(max(abs(x2 - x1), abs(y2 - y1)) * 2) + 1
        for i in range(steps + 1):
            t = i / steps
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            dot(x, y, width / 2.0, color)

    # 三个节点：左上、左下、右中（汇合）
    ax, ay = s * 0.28, s * 0.28
    bx, by = s * 0.28, s * 0.72
    cx2, cy2 = s * 0.72, s * 0.50
    nr = max(1.4, s * 0.085)

    line(ax, ay, cx2, cy2, max(1.2, s * 0.045), LINK)
    line(bx, by, cx2, cy2, max(1.2, s * 0.045), LINK)
    dot(ax, ay, nr, NODE)
    dot(bx, by, nr, NODE)
    dot(cx2, cy2, nr * 1.15, ACCENT)

    # 转 BMP 行序（自下而上）+ BGRA
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            r_, g_, b_, a_ = px[y][x]
            row += bytes((b_, g_, r_, a_))
        rows.append(bytes(row))
    return b"".join(rows)


def build_ico(path: str, sizes=(16, 24, 32, 48, 64, 128, 256)):
    images = []
    for s in sizes:
        data = render(s)
        # BITMAPINFOHEADER，高度写两倍（XOR + AND 掩码）
        hdr = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(data), 0, 0, 0, 0)
        mask_row = ((s + 31) // 32) * 4
        mask = b"\x00" * (mask_row * s)
        images.append(hdr + data + mask)

    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    for img in images:
        out += img

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(out))
    return len(out)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "packaging/agenthub.ico"
    n = build_ico(target)
    print(f"已生成 {target}（{n} 字节）")
