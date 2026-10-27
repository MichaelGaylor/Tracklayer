"""Exact geometry for the shape-based engine.

Obstacles are polygons already grown by the clearance a track needs, so a track centreline that
does not enter any polygon is legal. Arcs are circumscribed (the polygon lies outside the true
arc), which keeps the result conservative by a few microns rather than a grid pitch.
"""
from __future__ import annotations

import math

import numpy as np

Pt = tuple[float, float]
SLACK = 0.004          # mm added to every expansion so "exactly at clearance" never rounds the wrong way
ARC_N = 16             # chords per full circle on pads and vias
CAP_N = 12             # chords per full circle on track ends


def _arc(cx: float, cy: float, r: float, a0: float, a1: float, n: int) -> list[Pt]:
    """Points on a circumscribed arc from angle a0 to a1 (radians), n chords."""
    if n < 1:
        return [(cx + r * math.cos(a0), cy + r * math.sin(a0))]
    step = (a1 - a0) / n
    rr = r / math.cos(step / 2)
    return [(cx + rr * math.cos(a0 + (k + 0.5) * step), cy + rr * math.sin(a0 + (k + 0.5) * step)) for k in range(n)]


def rrect_polygon(cx: float, cy: float, w: float, h: float, rot_deg: float, corner: float, expand: float) -> list[Pt]:
    """A rounded rectangle (pad) grown by `expand`, as a polygon (counter-clockwise in a y-up sense)."""
    corner = max(0.0, min(corner, w / 2, h / 2))
    hw, hh = w / 2 - corner, h / 2 - corner        # straight half extents
    R = corner + expand
    if R <= 1e-9:
        local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    else:
        n = max(2, ARC_N // 4)
        local = []
        local += _arc(hw, hh, R, 0.0, math.pi / 2, n)
        local += _arc(-hw, hh, R, math.pi / 2, math.pi, n)
        local += _arc(-hw, -hh, R, math.pi, 1.5 * math.pi, n)
        local += _arc(hw, -hh, R, 1.5 * math.pi, 2 * math.pi, n)
    a = math.radians(rot_deg)
    c, s = math.cos(a), math.sin(a)
    # local -> board uses the same rotation convention as the pad positions (see board.rot_local)
    return [(cx + x * c + y * s, cy - x * s + y * c) for x, y in local]


def capsule_polygon(x1: float, y1: float, x2: float, y2: float, r: float) -> list[Pt]:
    """A track segment grown to radius r (half width plus clearance)."""
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return circle_polygon(x1, y1, r, CAP_N)
    ang = math.atan2(dy, dx)
    n = max(2, CAP_N // 2)
    pts = _arc(x2, y2, r, ang - math.pi / 2, ang + math.pi / 2, n)
    pts += _arc(x1, y1, r, ang + math.pi / 2, ang + 1.5 * math.pi, n)
    return pts


def circle_polygon(cx: float, cy: float, r: float, n: int = ARC_N) -> list[Pt]:
    return _arc(cx, cy, r, 0.0, 2 * math.pi, n)


def polygon_grown(pts: list[Pt], expand: float) -> list[list[Pt]]:
    """A general polygon grown by `expand`: the polygon itself plus a capsule per edge (a union,
    which is all the blocking tests need)."""
    out = [list(pts)]
    if expand > 0:
        n = len(pts)
        for k in range(n):
            (ax, ay), (bx, by) = pts[k], pts[(k + 1) % n]
            out.append(capsule_polygon(ax, ay, bx, by, expand))
    return out


def point_in_polygon(x: float, y: float, pts: list[Pt]) -> bool:
    inside = False
    n = len(pts)
    for k in range(n):
        (ax, ay), (bx, by) = pts[k], pts[(k + 1) % n]
        if (ay > y) != (by > y):
            xi = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < xi:
                inside = not inside
    return inside


def polygon_area(pts: list[Pt]) -> float:
    a = 0.0
    n = len(pts)
    for k in range(n):
        (x1, y1), (x2, y2) = pts[k], pts[(k + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2


def is_convex(pts: list[Pt]) -> bool:
    n = len(pts)
    sign = 0
    for k in range(n):
        (ax, ay), (bx, by), (cx, cy) = pts[k], pts[(k + 1) % n], pts[(k + 2) % n]
        cr = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cr) < 1e-12:
            continue
        s = 1 if cr > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


class Obstacles:
    """All grown obstacle polygons on one layer for one route class, with a bucket index over
    their edges so a visibility test only looks at the edges near the segment."""

    def __init__(self, bucket: float = 2.0):
        self.bucket = bucket
        self.polys: list[list[Pt]] = []
        self.poly_net: list[int] = []
        self.poly_convex: list[bool] = []
        self._edges: list[tuple[float, float, float, float, int, int]] = []   # ax ay bx by poly net
        self.buckets: dict[tuple[int, int], list[int]] = {}
        self.pbuckets: dict[tuple[int, int], list[int]] = {}   # polygons by the buckets their bbox covers
        self._arr = None
        # vertex nodes
        self.vx: list[float] = []
        self.vy: list[float] = []
        self.vpoly: list[int] = []
        self.vidx: list[int] = []
        self.vprev: list[Pt] = []
        self.vnext: list[Pt] = []
        self.vbuckets: dict[tuple[int, int], list[int]] = {}
        self._varr = None

    def add(self, pts: list[Pt], net: int) -> int:
        pid = len(self.polys)
        if polygon_area(pts) < 0:
            pts = pts[::-1]
        self.polys.append(pts)
        self.poly_net.append(net)
        self.poly_convex.append(is_convex(pts))
        n = len(pts)
        b = self.bucket
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        for i in range(int(math.floor(min(xs) / b)), int(math.floor(max(xs) / b)) + 1):
            for j in range(int(math.floor(min(ys) / b)), int(math.floor(max(ys) / b)) + 1):
                self.pbuckets.setdefault((i, j), []).append(pid)
        for k in range(n):
            (ax, ay), (bx, by) = pts[k], pts[(k + 1) % n]
            eid = len(self._edges)
            self._edges.append((ax, ay, bx, by, pid, net))
            for i in range(int(math.floor(min(ax, bx) / b)), int(math.floor(max(ax, bx) / b)) + 1):
                for j in range(int(math.floor(min(ay, by) / b)), int(math.floor(max(ay, by) / b)) + 1):
                    self.buckets.setdefault((i, j), []).append(eid)
        # vertex nodes, nudged a hair outwards so they are strictly outside the polygon
        for k in range(n):
            (px, py), (x, y), (nx_, ny_) = pts[k - 1], pts[k], pts[(k + 1) % n]
            d1x, d1y = x - px, y - py
            d2x, d2y = nx_ - x, ny_ - y
            l1 = math.hypot(d1x, d1y) or 1.0
            l2 = math.hypot(d2x, d2y) or 1.0
            # normals of the two edges (one of the two orientations is outward)
            n1x, n1y = d1y / l1, -d1x / l1
            n2x, n2y = d2y / l2, -d2x / l2
            ox, oy = n1x + n2x, n1y + n2y
            lo = math.hypot(ox, oy) or 1.0
            ox, oy = ox / lo * 0.002, oy / lo * 0.002
            if point_in_polygon(x + ox, y + oy, pts):
                ox, oy = -ox, -oy
            vid = len(self.vx)
            self.vx.append(x + ox); self.vy.append(y + oy); self.vpoly.append(pid); self.vidx.append(k)
            self.vprev.append((px, py)); self.vnext.append((nx_, ny_))
            self.vbuckets.setdefault((int(math.floor((x + ox) / b)), int(math.floor((y + oy) / b))), []).append(vid)
        self._arr = None
        self._varr = None
        return pid

    def _arrays(self):
        if self._arr is None:
            e = np.array(self._edges, dtype=np.float64).reshape(-1, 6)
            self._arr = (e[:, 0], e[:, 1], e[:, 2], e[:, 3], e[:, 4].astype(np.int64), e[:, 5].astype(np.int64))
        return self._arr

    def vertices(self):
        """(x, y, polygon id, vertex index, prev x, prev y, next x, next y) as arrays."""
        if self._varr is None:
            pv = np.array(self.vprev, dtype=np.float64).reshape(-1, 2)
            nx = np.array(self.vnext, dtype=np.float64).reshape(-1, 2)
            self._varr = (np.array(self.vx), np.array(self.vy), np.array(self.vpoly, dtype=np.int64),
                          np.array(self.vidx, dtype=np.int64), pv[:, 0], pv[:, 1], nx[:, 0], nx[:, 1])
        return self._varr

    def edges_near(self, x1: float, y1: float, x2: float, y2: float):
        """Edge ids (numpy array, unique) in the buckets covering the bbox."""
        b = self.bucket
        parts = []
        for i in range(int(math.floor(min(x1, x2) / b)), int(math.floor(max(x1, x2) / b)) + 1):
            for j in range(int(math.floor(min(y1, y2) / b)), int(math.floor(max(y1, y2) / b)) + 1):
                lst = self.buckets.get((i, j))
                if lst:
                    parts.append(lst)
        if not parts:
            return np.zeros(0, dtype=np.int64)
        if len(parts) == 1:
            return np.array(parts[0], dtype=np.int64)
        return np.unique(np.concatenate([np.array(p_, dtype=np.int64) for p_ in parts]))

    def vertices_near(self, x: float, y: float, want: int, max_radius: float = 30.0) -> np.ndarray:
        """Ids of the vertices in the bucket rings around (x, y), growing the ring until at least
        `want` are gathered (or the radius runs out). Not sorted by distance."""
        b = self.bucket
        ci, cj = int(math.floor(x / b)), int(math.floor(y / b))
        parts = []
        count = 0
        r = 0
        while r * b <= max_radius:
            for i in range(ci - r, ci + r + 1):
                for j in range(cj - r, cj + r + 1):
                    if max(abs(i - ci), abs(j - cj)) != r:
                        continue
                    lst = self.vbuckets.get((i, j))
                    if lst:
                        parts.append(lst); count += len(lst)
            if count >= want:
                break
            r += 1
        if not parts:
            return np.zeros(0, dtype=np.int64)
        return np.concatenate([np.array(p_, dtype=np.int64) for p_ in parts])

    def blocked(self, x1: float, y1: float, x2: float, y2: float, net: int, eps: float = 1e-7) -> bool:
        """Does the open segment cross into any polygon of another net?"""
        idx = self.edges_near(x1, y1, x2, y2)
        if len(idx) == 0:
            return False
        ax, ay, bx, by, pid, enet = self._arrays()
        m = enet[idx] != net
        if not m.any():
            return False
        idx = idx[m]
        ax, ay, bx, by = ax[idx], ay[idx], bx[idx], by[idx]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        Ls = math.sqrt(L2) or 1.0
        # signed distances (not raw cross products): a segment and a chord both well under a tenth of a millimetre
        # must still count as crossing
        o1 = (dx * (ay - y1) - dy * (ax - x1)) / Ls   # edge start relative to the segment line
        o2 = (dx * (by - y1) - dy * (bx - x1)) / Ls
        ex, ey = bx - ax, by - ay
        Le = np.hypot(ex, ey)
        Le[Le < 1e-12] = 1.0
        o3 = (ex * (y1 - ay) - ey * (x1 - ax)) / Le   # segment start relative to the edge line
        o4 = (ex * (y2 - ay) - ey * (x2 - ax)) / Le
        proper = (o1 * o2 < -eps) & (o3 * o4 < -eps)
        if proper.any():
            return True
        # touching: an edge end lies on the segment away from its ends, or a segment end lies on an edge
        tol = 1e-4
        for ox, oy, oo in ((ax, ay, o1), (bx, by, o2)):
            on_line = np.abs(oo) < eps
            if on_line.any():
                t = ((ox - x1) * dx + (oy - y1) * dy) / max(L2, 1e-18)
                inner = on_line & (t > tol) & (t < 1 - tol) & (o3 * o4 <= 0)
                if inner.any():
                    return True
        return False

    def blocked_many(self, x1: float, y1: float, X2: np.ndarray, Y2: np.ndarray, net: int,
                     eps: float = 1e-7) -> np.ndarray:
        """For segments from (x1, y1) to each (X2[k], Y2[k]): does it cross into another net's polygon?"""
        K = len(X2)
        out = np.zeros(K, dtype=bool)
        if K == 0:
            return out
        idx = self.edges_near(min(x1, float(X2.min())), min(y1, float(Y2.min())), max(x1, float(X2.max())), max(y1, float(Y2.max())))
        if len(idx) == 0:
            return out
        ax, ay, bx, by, pid, enet = self._arrays()
        idx = idx[enet[idx] != net]
        if len(idx) == 0:
            return out
        ax, ay, bx, by = ax[idx], ay[idx], bx[idx], by[idx]
        exmin, exmax = np.minimum(ax, bx), np.maximum(ax, bx)
        eymin, eymax = np.minimum(ay, by), np.maximum(ay, by)
        ex, ey = bx - ax, by - ay
        Le = np.hypot(ex, ey)
        Le[Le < 1e-12] = 1.0
        o3 = (ex * (y1 - ay) - ey * (x1 - ax)) / Le
        tol = 1e-4
        chunk = max(1, int(2_000_000 // max(len(idx), 1)))
        for c0 in range(0, K, chunk):
            xs, ys = X2[c0:c0 + chunk], Y2[c0:c0 + chunk]
            dx, dy = xs - x1, ys - y1
            L2 = dx * dx + dy * dy
            L = np.sqrt(L2)
            Ls = np.where(L < 1e-12, 1.0, L)
            # bbox pre-filter
            ov = ((np.minimum(x1, xs)[:, None] <= exmax[None, :]) & (np.maximum(x1, xs)[:, None] >= exmin[None, :]) &
                  (np.minimum(y1, ys)[:, None] <= eymax[None, :]) & (np.maximum(y1, ys)[:, None] >= eymin[None, :]))
            o1 = (dx[:, None] * (ay - y1)[None, :] - dy[:, None] * (ax - x1)[None, :]) / Ls[:, None]
            o2 = (dx[:, None] * (by - y1)[None, :] - dy[:, None] * (bx - x1)[None, :]) / Ls[:, None]
            o4 = (ex[None, :] * (ys[:, None] - ay[None, :]) - ey[None, :] * (xs[:, None] - ax[None, :])) / Le[None, :]
            o34 = o3[None, :] * o4
            hit = ov & (o1 * o2 < -eps) & (o34 < -eps)
            # an edge end touching the segment away from its ends
            for ox, oy, oo in ((ax, ay, o1), (bx, by, o2)):
                on_line = ov & (np.abs(oo) < eps)
                if on_line.any():
                    t = ((ox[None, :] - x1) * dx[:, None] + (oy[None, :] - y1) * dy[:, None]) / np.maximum(L2, 1e-18)[:, None]
                    hit |= on_line & (t > tol) & (t < 1 - tol) & (o34 <= 0)
            out[c0:c0 + chunk] = hit.any(axis=1)
        return out

    def contains(self, x: float, y: float, net: int) -> int:
        """Index of a polygon of another net containing the point, or -1."""
        b = self.bucket
        for pid in self.pbuckets.get((int(math.floor(x / b)), int(math.floor(y / b))), ()):
            if self.poly_net[pid] == net:
                continue
            if point_in_polygon(x, y, self.polys[pid]):
                return pid
        return -1
