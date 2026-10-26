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
    out: dict[int, tuple[list, list]] = {}
    for ref, pads in by_ref.items():
        if len(pads) < 2:
            continue
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        # which pads are hemmed in, and by which neighbour
        order = sorted(pads, key=lambda p: (round(p.y, 3), round(p.x, 3)))
        k = 0
        for p in order:
            if p.through or p.net <= 0 or p.net not in nets or not any(l in routed for l in p.layers):
                continue
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
            # stagger: alternate pins along the row take the near and the far position, then try further out
            base = edge + vd / 2 + cl
            tries = [base + (k % 2) * pitch, base + ((k + 1) % 2) * pitch, base + 2 * pitch, base + 3 * pitch]
            k += 1
            via_at = None
            for dist in tries:
                vx, vy = p.x + ox * dist, p.y + oy * dist
                if not (bx0 + vd < vx < bx1 - vd and by0 + vd < vy < by1 - vd):
                    continue
                ok = True
                for q2 in b.pads:
                    if q2.net == p.net:
                        continue
                    need = vd / 2 + min(q2.w, q2.h) / 2 + cl
                    if math.hypot(q2.x - vx, q2.y - vy) < need:
                        ok = False
                        break
                if ok:
                    for (ex, ey, es) in placed:
                        if math.hypot(ex - vx, ey - vy) < (vd + es) / 2 + cl:
                            ok = False
                            break
                if ok:
                    via_at = (vx, vy, dist)
                    break
            segs, vias = out.setdefault(p.net, ([], []))
            if via_at is None:
                # no room for a via: a bare stub still reserves the escape and gives the router a point to start from
                dist = edge + stub_min
                ex, ey = p.x + ox * dist, p.y + oy * dist
                segs.append((layer, p.x, p.y, ex, ey, neck))
                b.tracks.append(Track(p.x, p.y, ex, ey, neck, layer, p.net))
                continue
            vx, vy, dist = via_at
            segs.append((layer, p.x, p.y, vx, vy, neck))
            vias.append((vx, vy))
            b.tracks.append(Track(p.x, p.y, vx, vy, neck, layer, p.net))
            b.vias.append(Via(vx, vy, vd, vdr, p.net, list(b.copper_layers)))
            placed.append((vx, vy, vd))
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
