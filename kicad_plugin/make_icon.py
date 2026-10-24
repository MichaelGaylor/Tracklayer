"""Draws the plugin icon (two tracks and a via) at any size. Pure Python, no PIL.

    python make_icon.py            -> icon.png (24 px, toolbar)
    import make_icon; make_icon.draw(64, path)
"""
import os
import struct
import sys
import zlib


def draw(size: int, path: str) -> None:
    W = H = size
    k = size / 24.0
    px = [[(0, 0, 0, 0) for _ in range(W)] for _ in range(H)]

    def blob(cx, cy, r, col):
        cx, cy, r = cx * k, cy * k, r * k
        for y in range(max(0, int(cy - r - 1)), min(H, int(cy + r + 2))):
            for x in range(max(0, int(cx - r - 1)), min(W, int(cx + r + 2))):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    px[y][x] = col

    def seg(x1, y1, x2, y2, r, col):
        n = int(max(abs(x2 - x1), abs(y2 - y1)) * 2 * k) + 1
        for i in range(n + 1):
            t = i / n
            blob(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r, col)

    red = (216, 50, 60, 255)
    blue = (56, 100, 216, 255)
    seg(3, 20, 10, 13, 1.6, blue)
    seg(10, 13, 10, 6, 1.6, blue)
    seg(10, 6, 20, 6, 1.6, blue)
    seg(3, 5, 9, 11, 1.6, red)
    seg(9, 11, 15, 11, 1.6, red)
    seg(15, 11, 21, 17, 1.6, red)
    blob(15, 11, 3.2, (235, 235, 235, 255))
    blob(15, 11, 1.4, (40, 40, 40, 255))

    raw = b''.join(b'\x00' + b''.join(struct.pack('BBBB', *p) for p in row) for row in px)

    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 6, 0, 0, 0)) \
        + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.png')
    draw(int(sys.argv[1]) if len(sys.argv) > 1 else 24, out)
    print(out, 'written')
