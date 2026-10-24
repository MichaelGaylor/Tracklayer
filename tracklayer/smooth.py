"""Turn a staircase of grid cells into a few straight segments.

`clear(x1, y1, x2, y2)` says whether a track may run straight between two points. Modes:
  '45'  segments at multiples of 45 degrees (the usual PCB look)
  'any' straight lines at any angle (shortest, fewest segments)
  '90'  orthogonal only
"""
from __future__ import annotations

import math

Pt = tuple[float, float]


def _collinear(a: Pt, b: Pt, c: Pt, eps: float = 1e-6) -> bool:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < eps


def _merge(pts: list[Pt]) -> list[Pt]:
    out: list[Pt] = []
    for p in pts:
        if out and math.dist(out[-1], p) < 1e-6:
            continue
        if len(out) >= 2 and _collinear(out[-2], out[-1], p):
            out[-1] = p
        else:
            out.append(p)
    return out


def _canonical(a: Pt, b: Pt, pitch: float) -> list[list[Pt]]:
    """Ways to join a and b with one 45-degree diagonal and one axis run (both orders)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    adx, ady = abs(dx), abs(dy)
    if adx < 1e-9 or ady < 1e-9 or abs(adx - ady) < 1e-9:
        return [[a, b]]
    d = min(adx, ady)
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    # diagonal first, then straight
    m1 = (a[0] + sx * d, a[1] + sy * d)
    # straight first, then diagonal
    m2 = (b[0] - sx * d, b[1] - sy * d)
    return [[a, m1, b], [a, m2, b]]


def smooth_polyline(pts: list[Pt], clear, mode: str, pitch: float) -> list[Pt]:
    pts = _merge(pts)
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    i = 0
    n = len(pts)
    while i < n - 1:
        best = None
        # farthest point reachable; try from the end backwards so the first hit is the farthest
        for k in range(n - 1, i, -1):
            a, b = pts[i], pts[k]
            if mode == 'any':
                if clear(*a, *b):
                    best = [b]
                    break
            elif mode == '45':
                for cand in _canonical(a, b, pitch):
                    ok = all(clear(*p, *q) for p, q in zip(cand, cand[1:]))
                    if ok:
                        best = cand[1:]
                        break
                if best:
                    break
            else:  # '90'
                if (abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9) and clear(*a, *b):
                    best = [b]
                    break
                if k > i + 1:
                    continue
                best = [b]  # adjacent grid cells are always fine
                break
        if best is None:
            best = [pts[i + 1]]
            k = i + 1
        out += best
        i = k
    return _merge(out)
