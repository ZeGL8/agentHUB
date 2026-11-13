# -*- coding: utf-8 -*-
"""生成 macOS 的 agenthub.icns：复用 make_icon.render()（与 Windows .ico 同一份设计），
写出 iconset PNG，再交给系统自带的 iconutil 合成。只能在 macOS 上运行。"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_icon import render  # noqa: E402


def png(size: int) -> bytes:
    """render() 给的是 BMP 行序（自下而上）+ BGRA，转成 PNG 需要的自上而下 RGBA。"""
    raw, stride = render(size), size * 4
    rows = []
    for y in range(size - 1, -1, -1):
        row = raw[y * stride:(y + 1) * stride]
        rgba = bytearray(row)
        rgba[0::4], rgba[2::4] = row[2::4], row[0::4]
        rows.append(b"\x00" + bytes(rgba))
    chunk = lambda tag, data: (struct.pack(">I", len(data)) + tag + data
                               + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def main(out: str) -> None:
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "agenthub.iconset")
    os.makedirs(iconset)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{base}x{base}" + ("@2x" if scale == 2 else "") + ".png"
            with open(os.path.join(iconset, name), "wb") as f:
                f.write(png(base * scale))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    shutil.rmtree(tmp)
    print("已生成", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         os.path.join(os.path.dirname(os.path.abspath(__file__)), "agenthub.icns"))
