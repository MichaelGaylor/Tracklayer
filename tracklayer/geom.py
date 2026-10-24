"""Rasterising board copper onto a grid with numpy.

The grid is a 2-D array [rows (y), cols (x)]; cell (j, i) is centred at
(x0 + i * pitch, y0 + j * pitch). Each fill function returns (j0, i0, mask) for a small window
so callers can paint into bigger arrays cheaply.
"""
from __future__ import annotations

import math

import numpy as np

Pt = tuple[float, float]


class Grid:
    def __init__(self, x0: float, y0: float, x1: float, y1: float, pitch: float):
        self.pitch = pitch
        self.x0, self.y0 = x0, y0
        self.nx = int(math.ceil((x1 - x0) / pitch)) + 1
        self.ny = int(math.ceil((y1 - y0) / pitch)) + 1

    def to_cell(self, x: float, y: float) -> tuple[int, int]:
        """Nearest cell (j, i) to a point."""
        return int(round((y - self.y0) / self.pitch)), int(round((x - self.x0) / self.pitch))

    def to_xy(self, j: int, i: int) -> Pt:
        return self.x0 + i * self.pitch, self.y0 + j * self.pitch

    def window(self, xmin: float, ymin: float, xmax: float, ymax: float):
        """Cell index window [j0:j1, i0:i1] covering a bbox, clipped to the grid, plus cell centres."""
        i0 = max(0, int(math.floor((xmin - self.x0) / self.pitch)))
        i1 = min(self.nx, int(math.ceil((xmax - self.x0) / self.pitch)) + 1)
        j0 = max(0, int(math.floor((ymin - self.y0) / self.pitch)))
        j1 = min(self.ny, int(math.ceil((ymax - self.y0) / self.pitch)) + 1)
        if i1 <= i0 or j1 <= j0:
            return j0, i0, None, None
        xs = self.x0 + np.arange(i0, i1) * self.pitch
        ys = self.y0 + np.arange(j0, j1) * self.pitch
        X, Y = np.meshgrid(xs, ys)
        return j0, i0, X, Y


def rrect_mask(g: Grid, cx: float, cy: float, w: float, h: float, rot_deg: float, corner: float, expand: float):
    """Cells whose centre is within `expand` of a rotated rounded rectangle (negative expand shrinks)."""
    half = math.hypot(w, h) / 2 + max(expand, 0.0) + g.pitch
    j0, i0, X, Y = g.window(cx - half, cy - half, cx + half, cy + half)
    if X is None:
        return j0, i0, None
    a = math.radians(rot_deg)
    c, s = math.cos(a), math.sin(a)
    dx, dy = X - cx, Y - cy
    # board -> pad local: inverse of rot_local
    lx = dx * c - dy * s
    ly = dx * s + dy * c
    corner = min(corner, w / 2, h / 2)
    qx = np.abs(lx) - (w / 2 - corner)
    qy = np.abs(ly) - (h / 2 - corner)
    d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - corner
    return j0, i0, d <= expand


def capsule_mask(g: Grid, x1: float, y1: float, x2: float, y2: float, r: float):
    """Cells within r of the segment (x1,y1)-(x2,y2)."""
    xmin, xmax = min(x1, x2) - r - g.pitch, max(x1, x2) + r + g.pitch
    ymin, ymax = min(y1, y2) - r - g.pitch, max(y1, y2) + r + g.pitch
    j0, i0, X, Y = g.window(xmin, ymin, xmax, ymax)
    if X is None:
        return j0, i0, None
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    if L2 < 1e-12:
        d = np.hypot(X - x1, Y - y1)
    else:
        t = np.clip(((X - x1) * vx + (Y - y1) * vy) / L2, 0.0, 1.0)
        d = np.hypot(X - (x1 + t * vx), Y - (y1 + t * vy))
    return j0, i0, d <= r


def circle_mask(g: Grid, cx: float, cy: float, r: float):
    return capsule_mask(g, cx, cy, cx, cy, r)


def polygon_mask(g: Grid, pts: list[Pt], expand: float = 0.0):
    """Cells inside a polygon (even-odd rule), grown by `expand` along its edges when expand > 0,
    shrunk by |expand| when negative. Scanline parity, so thousands of vertices are fine."""
    if len(pts) < 3:
        return 0, 0, None
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    e = max(expand, 0.0)
    j0, i0, X, Y = g.window(min(xs) - e, min(ys) - e, max(xs) + e, max(ys) + e)
    if X is None:
        return j0, i0, None
    H, W = X.shape
    toggles = np.zeros((H, W + 1), dtype=np.uint8)
    n = len(pts)
    p_ = g.pitch
    for k in range(n):
        (ax, ay), (bx, by) = pts[k], pts[(k + 1) % n]
        if ay == by:
            continue
        ylo, yhi = (ay, by) if ay < by else (by, ay)
        # rows with ylo <= Y < yhi
        r0 = int(math.ceil((ylo - g.y0) / p_)) - j0
        r1 = int(math.ceil((yhi - g.y0) / p_)) - j0
        r0 = max(r0, 0); r1 = min(r1, H)
        if r1 <= r0:
            continue
        rows = np.arange(r0, r1)
        yv = g.y0 + (rows + j0) * p_
        xint = ax + (yv - ay) * (bx - ax) / (by - ay)
        # cells with X < xint are flipped: X = x0 + (i0 + i) * p  ->  i < (xint - x0) / p - i0
        ci = np.ceil((xint - g.x0) / p_ - i0).astype(np.int64)
        ci = np.clip(ci, 0, W)
        np.bitwise_xor.at(toggles, (rows, ci), 1)
    # inside[j, i] = parity of toggles[j, i+1 ..]  (reverse exclusive cumulative xor)
    acc = np.cumsum(toggles[:, ::-1], axis=1)[:, ::-1] & 1
    inside = acc[:, 1:].astype(bool)
    if e > 0:
        for k in range(n):
            (ax, ay), (bx, by) = pts[k], pts[(k + 1) % n]
            jj, ii, m = capsule_mask(g, ax, ay, bx, by, e)
            if m is not None:
                _or_into(inside, j0, i0, jj, ii, m)
    elif expand < 0:
        for k in range(n):
            (ax, ay), (bx, by) = pts[k], pts[(k + 1) % n]
            jj, ii, m = capsule_mask(g, ax, ay, bx, by, -expand)
            if m is not None:
                _andnot_into(inside, j0, i0, jj, ii, m)
    return j0, i0, inside


def _or_into(dst, dj, di, sj, si, m):
    """dst[j0..] |= m, where dst starts at (dj, di) and m at (sj, si)."""
    j0 = max(dj, sj); i0 = max(di, si)
    j1 = min(dj + dst.shape[0], sj + m.shape[0]); i1 = min(di + dst.shape[1], si + m.shape[1])
    if j1 > j0 and i1 > i0:
        dst[j0 - dj:j1 - dj, i0 - di:i1 - di] |= m[j0 - sj:j1 - sj, i0 - si:i1 - si]


def _andnot_into(dst, dj, di, sj, si, m):
    j0 = max(dj, sj); i0 = max(di, si)
    j1 = min(dj + dst.shape[0], sj + m.shape[0]); i1 = min(di + dst.shape[1], si + m.shape[1])
    if j1 > j0 and i1 > i0:
        dst[j0 - dj:j1 - dj, i0 - di:i1 - di] &= ~m[j0 - sj:j1 - sj, i0 - si:i1 - si]


def paint_owner(owner: np.ndarray, j0: int, i0: int, mask, net: int, ALL: int = -2):
    """Mark cells as owned by `net`; a cell with two different owners becomes ALL (blocked for everyone)."""
    if mask is None:
        return
    win = owner[j0:j0 + mask.shape[0], i0:i0 + mask.shape[1]]
    m = mask[:win.shape[0], :win.shape[1]]
    other = m & (win != -1) & (win != net)
    win[m & (win == -1)] = net
    win[other] = ALL


def set_mask(dst: np.ndarray, j0: int, i0: int, mask, value=True):
    if mask is None:
        return
    win = dst[j0:j0 + mask.shape[0], i0:i0 + mask.shape[1]]
    win[mask[:win.shape[0], :win.shape[1]]] = value


def add_mask(dst: np.ndarray, j0: int, i0: int, mask, value: float):
    if mask is None:
        return
    win = dst[j0:j0 + mask.shape[0], i0:i0 + mask.shape[1]]
    win[mask[:win.shape[0], :win.shape[1]]] += value


def cells_of(j0: int, i0: int, mask) -> list[tuple[int, int]]:
    if mask is None:
        return []
    js, is_ = np.nonzero(mask)
    return list(zip((js + j0).tolist(), (is_ + i0).tolist()))
