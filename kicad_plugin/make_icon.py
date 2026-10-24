"""Draws icon.png (24x24): two tracks and a via, in Tracklayer's colours. Pure Python, no PIL."""
import os
import struct
import zlib

W = H = 24
px = [[(0, 0, 0, 0) for _ in range(W)] for _ in range(H)]


def blob(cx, cy, r, col):
    for y in range(H):
        for x in range(W):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                px[y][x] = col


def seg(x1, y1, x2, y2, r, col):
    n = int(max(abs(x2 - x1), abs(y2 - y1)) * 2) + 1
    for k in range(n + 1):
        t = k / n
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
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.png'), 'wb').write(png)
print('icon.png written')
