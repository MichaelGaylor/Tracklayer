"""Fan-out before routing: every fine-pitch SMD pad gets an escape stub.

A pin on a SOIC or SOT-23 has neighbours too close for a full-width track plus two clearances
to pass between, so whichever net reaches the row first at full width walls the pins beside it
in. The usual answer is to fan the part out first: a short stub at the neck width straight out
from each such pad, ending in a via, with the vias staggered along the row so they fit. The nets
then route from the vias, where there is room. The stubs and vias are added to the board as
existing copper of their nets (so both engines see them) and handed back for the writer.
"""
from __future__ import annotations

import math

from .board import Board, Pad, Track, Via


def _rot(dx: float, dy: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return dx * c - dy * s, dx * s + dy * c


def _extent(p: Pad, ux: float, uy: float) -> float:
    """Half-size of the pad along the unit direction (ux, uy)."""
    ax, ay = _rot(1.0, 0.0, p.rot)       # the pad's own x axis on the board
    along = abs(ux * ax + uy * ay)
    across = abs(-ux * ay + uy * ax)
    return (along * p.w + across * p.h) / 2


def _seg_seg_cross(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    def orient(px, py, qx, qy, rx, ry):
        v = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        return (v > 1e-12) - (v < -1e-12)
    o1, o2 = orient(ax, ay, bx, by, cx, cy), orient(ax, ay, bx, by, dx, dy)
    o3, o4 = orient(cx, cy, dx, dy, ax, ay), orient(cx, cy, dx, dy, bx, by)
    return o1 != o2 and o3 != o4


def _pad_seg_dist(q: Pad, x1: float, y1: float, x2: float, y2: float) -> float:
    """Distance from a segment to the pad's rectangle (0 when they touch), in the pad's own frame.
    Both shapes are convex, so the distance is attained at a corner of one against the other."""
    a = math.radians(-q.rot)
    c, s = math.cos(a), math.sin(a)

    def tf(x, y):
        dx, dy = x - q.x, y - q.y
        return dx * c - dy * s, dx * s + dy * c

    (ax, ay), (bx, by) = tf(x1, y1), tf(x2, y2)
    hw, hh = q.w / 2, q.h / 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    inside = lambda x, y: -hw <= x <= hw and -hh <= y <= hh
    if inside(ax, ay) or inside(bx, by):
        return 0.0
    for i in range(4):
        (cx, cy), (dx, dy) = corners[i], corners[(i + 1) % 4]
        if _seg_seg_cross(ax, ay, bx, by, cx, cy, dx, dy):
            return 0.0
    best = min(_seg_dist(cx, cy, ax, ay, bx, by) for cx, cy in corners)
    for px, py in ((ax, ay), (bx, by)):
        ex = max(abs(px) - hw, 0.0)
        ey = max(abs(py) - hh, 0.0)
        best = min(best, math.hypot(ex, ey))
    return best


def _pad_point_dist(q: Pad, x: float, y: float) -> float:
    """Distance from a point to the pad's rectangle (0 inside)."""
    return _pad_seg_dist(q, x, y, x, y)


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_seg_dist(a, b):
    """Distance between two short segments: the least endpoint-to-segment distance (exact unless they cross)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return min(_seg_dist(ax1, ay1, *b), _seg_dist(ax2, ay2, *b), _seg_dist(bx1, by1, *a), _seg_dist(bx2, by2, *a))


def _already_connected(p: Pad, tracks: list, vias: list) -> bool:
    """True when a track end or a via of the pad's own net lies on the pad: it has its escape already."""
    for t in tracks:
        if t.net == p.net and t.layer in p.layers and (_pad_point_dist(p, t.x1, t.y1) <= 1e-3 or _pad_point_dist(p, t.x2, t.y2) <= 1e-3):
            return True
    for v in vias:
        if v.net == p.net and _pad_point_dist(p, v.x, v.y) <= 1e-3:
            return True
    return False


def fanout(b: Board, nets: set[int], neck_width: float | None = None, stub_min: float = 0.5,
           layers: list[str] | None = None) -> dict[int, tuple[list, list]]:
    """Add escape stubs for the fine-pitch pads of the given nets. Returns {net: (segments, vias)} in the
    writer's NetRoute format: segments (layer, x1, y1, x2, y2, width) and vias (x, y)."""
    neck = neck_width or b.netclasses['Default'].track_width
    routed = set(layers or b.copper_layers)
    by_ref: dict[str, list[Pad]] = {}
    for p in b.pads:
        by_ref.setdefault(p.ref, []).append(p)
    bx0, by0, bx1, by1 = b.bbox()
    placed: list[tuple[float, float, float]] = [(v.x, v.y, v.size) for v in b.vias]   # x, y, size of every via
    stubs: list[tuple[str, tuple, float, int, float]] = []   # layer, segment, width, net, clearance of every stub laid so far
    out: dict[int, tuple[list, list]] = {}
    existing = list(b.tracks)                                 # the copper already on the board, before any stub of ours
    existing_vias = list(b.vias)
    for ref, pads in by_ref.items():
        if len(pads) < 2:
            continue
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        # which pads are hemmed in, and by which neighbour
        order = sorted(pads, key=lambda p: (round(p.y, 3), round(p.x, 3)))
        kk: dict[tuple, int] = {}       # stagger index per escape direction: each pin row (column) alternates on its own
        for p in order:
            if p.through or p.net <= 0 or p.net not in nets or not any(l in routed for l in p.layers):
                continue
            if _already_connected(p, existing, existing_vias):
                continue                                           # a track or via of its net already sits on the pad
            nc = b.netclass(p.net)
            w, cl, vd, vdr = nc.track_width, nc.clearance, nc.via_diameter, nc.via_drill
            near = None
            for q in pads:
                if q is p or q.net == p.net:
                    continue
                d = math.hypot(q.x - p.x, q.y - p.y)
                if near is None or d < near[0]:
                    near = (d, q)
            if near is None:
                continue
            d, q = near
            rx, ry = (q.x - p.x) / d, (q.y - p.y) / d                # along the row
            gap = d - _extent(p, rx, ry) - _extent(q, rx, ry)
            if gap >= w + 2 * cl:
                continue                                           # the full width passes: nothing to do
            # outward: perpendicular to the row, away from the footprint centre
            ox, oy = -ry, rx
            if (p.x - cx) * ox + (p.y - cy) * oy < 0:
                ox, oy = -ox, -oy
            layer = next(l for l in p.layers if l in routed)
            edge = _extent(p, ox, oy)
            pitch = vd + cl
            # stagger: alternate pins along the row take the near and the far position, then try further out.
            # The index runs per escape direction, so the two rows of a SOIC or SOT-23 alternate independently:
            # counted across the whole part, three pins of one row could all land on the near slot and wall the
            # middle one in.
            dkey = (round(ox, 2), round(oy, 2))
            k = kk.get(dkey, 0)
            kk[dkey] = k + 1
            base = edge + vd / 2 + cl
            tries = [base + (k % 2) * pitch, base + ((k + 1) % 2) * pitch, base + 2 * pitch]
            others = [q2 for q2 in b.pads if q2.net != p.net and math.hypot(q2.x - p.x, q2.y - p.y) < 12.0]
            near_tracks = [t for t in existing if t.net != p.net
                           and _seg_dist(p.x, p.y, t.x1, t.y1, t.x2, t.y2) < 12.0]

            def clear(vx, vy, with_via):
                # the via and the whole stub must clear every other net's pad (its real rectangle) and every via so far,
                # by the larger of the two nets' clearances: KiCad's DRC takes the larger too
                for q2 in others:
                    cq = max(cl, b.clearance(q2.net)) if q2.net > 0 else cl
                    if with_via and _pad_point_dist(q2, vx, vy) < vd / 2 + cq:
                        return False
                    if _pad_seg_dist(q2, p.x, p.y, vx, vy) < neck / 2 + cq:
                        return False
                if with_via:
                    for (ex, ey, es) in placed:
                        if math.hypot(ex - vx, ey - vy) < (vd + es) / 2 + cl:
                            return False
                # ... and the tracks already on the board: the stub on its own layer, the via on every layer
                for t in near_tracks:
                    cq = max(cl, b.clearance(t.net)) if t.net > 0 else cl
                    if t.layer == layer and _seg_seg_dist((p.x, p.y, vx, vy), (t.x1, t.y1, t.x2, t.y2)) < neck / 2 + t.width / 2 + cq:
                        return False
                    if with_via and _seg_dist(vx, vy, t.x1, t.y1, t.x2, t.y2) < vd / 2 + t.width / 2 + cq:
                        return False
                # ... and every stub laid before it, by the larger of the two nets' clearances
                for (sl, sseg, sw, snet, scl) in stubs:
                    if snet == p.net or sl != layer:
                        continue
                    cq = max(cl, scl)
                    if _seg_seg_dist((p.x, p.y, vx, vy), sseg) < neck / 2 + sw / 2 + cq:
                        return False
                    if with_via and _seg_dist(vx, vy, *sseg) < vd / 2 + sw / 2 + cq:
                        return False
                return True

            via_at = None
            for dist in tries:
                vx, vy = p.x + ox * dist, p.y + oy * dist
                if not (bx0 + vd < vx < bx1 - vd and by0 + vd < vy < by1 - vd):
                    continue
                ok = clear(vx, vy, True)
                if ok:
                    via_at = (vx, vy, dist)
                    break
            segs, vias = out.setdefault(p.net, ([], []))
            if via_at is None:
                # no room for a via: a short bare stub still reserves the escape, if even that is clear
                dist = edge + stub_min
                ex, ey = p.x + ox * dist, p.y + oy * dist
                if clear(ex, ey, False):
                    segs.append((layer, p.x, p.y, ex, ey, neck))
                    b.tracks.append(Track(p.x, p.y, ex, ey, neck, layer, p.net))
                    stubs.append((layer, (p.x, p.y, ex, ey), neck, p.net, cl))
                continue
            vx, vy, dist = via_at
            segs.append((layer, p.x, p.y, vx, vy, neck))
            vias.append((vx, vy))
            b.tracks.append(Track(p.x, p.y, vx, vy, neck, layer, p.net))
            b.vias.append(Via(vx, vy, vd, vdr, p.net, list(b.copper_layers)))
            placed.append((vx, vy, vd))
            stubs.append((layer, (p.x, p.y, vx, vy), neck, p.net, cl))
    return out


def merge(routes: dict, fo: dict[int, tuple[list, list]], b: Board, make_route) -> None:
    """Put the fan-out copper into the routes dict so the writer emits it (also for nets left unrouted)."""
    for net, (segs, vias) in fo.items():
        r = routes.get(net)
        if r is None:
            r = make_route(net)
            nc = b.netclass(net)
            r.params = (nc.track_width, nc.clearance, nc.via_diameter, nc.via_drill)
            r.complete = False
            routes[net] = r
        r.segments = list(segs) + list(r.segments)
        r.vias = list(vias) + list(r.vias)
        if r.via_layers is None:
            r.via_layers = (b.copper_layers[0], b.copper_layers[-1])
