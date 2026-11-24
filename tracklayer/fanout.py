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


def _in_poly(x: float, y: float, pts: list) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _touch(a: tuple, b: tuple, strict: bool) -> bool:
    """Do two items of one net connect? Items are ('p', pad), ('s', layer, x1, y1, x2, y2, w), ('v', x, y, size)
    and ('z', zone). Strict is how KiCad joins them (a track end on the other item, a via centre on a pad);
    loose is any overlap of their copper, which is what conducts on the finished board."""
    eps = 1e-6
    if a[0] > b[0]:
        a, b = b, a
    ka, kb = a[0], b[0]
    if ka == 's' and kb == 's':
        if a[1] != b[1]:
            return False
        sa, sb = a[2:6], b[2:6]
        if strict:
            return (_seg_dist(a[2], a[3], *sb) <= b[6] / 2 + eps or _seg_dist(a[4], a[5], *sb) <= b[6] / 2 + eps
                    or _seg_dist(b[2], b[3], *sa) <= a[6] / 2 + eps or _seg_dist(b[4], b[5], *sa) <= a[6] / 2 + eps)
        return _seg_seg_cross(*sa, *sb) or _seg_seg_dist(sa, sb) < (a[6] + b[6]) / 2 - eps
    if ka == 's' and kb == 'v':
        if strict:
            return min(math.hypot(a[2] - b[1], a[3] - b[2]), math.hypot(a[4] - b[1], a[5] - b[2])) <= b[3] / 2 + eps
        return _seg_dist(b[1], b[2], *a[2:6]) < b[3] / 2 + a[6] / 2 - eps
    if ka == 'p' and kb == 's':
        if b[1] not in a[1].layers:
            return False
        if strict:
            return _pad_point_dist(a[1], b[2], b[3]) <= 1e-3 or _pad_point_dist(a[1], b[4], b[5]) <= 1e-3
        return _pad_seg_dist(a[1], *b[2:6]) < b[6] / 2 - eps
    if ka == 'p' and kb == 'v':
        return _pad_point_dist(a[1], b[1], b[2]) < (1e-3 if strict else b[3] / 2 - eps)
    if ka == 'v' and kb == 'v':
        d = math.hypot(a[1] - b[1], a[2] - b[2])
        return d <= max(a[3], b[3]) / 2 + eps if strict else d < (a[3] + b[3]) / 2 - eps
    if ka == 'v' and kb == 'z':
        return _in_poly(a[1], a[2], b[1].polygon)
    if ka == 'p' and kb == 'z':
        return a[1].through and _in_poly(a[1].x, a[1].y, b[1].polygon)
    if ka == 'p' and kb == 'p':
        # two pads of one footprint laid over each other (an SMD pad with a plated hole in it)
        return (any(l in b[1].layers for l in a[1].layers)
                and (_pad_point_dist(a[1], b[1].x, b[1].y) <= 1e-3 or _pad_point_dist(b[1], a[1].x, a[1].y) <= 1e-3))
    return False


def _end_held(items: list, i: int, ex: float, ey: float, alive: set) -> bool:
    """Is this end of segment i on something of its net: a pad, a via, another track, a pour on its layer?"""
    eps = 1e-6
    layer, w = items[i][1], items[i][6]
    for j in alive:
        if j == i:
            continue
        it = items[j]
        if it[0] == 'p':
            if layer in it[1].layers and _pad_point_dist(it[1], ex, ey) <= 1e-3:
                return True
        elif it[0] == 'v':
            if math.hypot(ex - it[1], ey - it[2]) <= it[3] / 2 + eps:
                return True
        elif it[0] == 'z':
            if layer in it[1].layers and _in_poly(ex, ey, it[1].polygon):
                return True
        elif it[1] == layer:
            if _seg_dist(ex, ey, *it[2:6]) <= it[6] / 2 + eps:
                return True
            if min(math.hypot(ex - it[2], ey - it[3]), math.hypot(ex - it[4], ey - it[5])) <= w / 2 + eps:
                return True
    return False


def _groups(adj: list, alive: set, anchors: set) -> frozenset:
    """How the anchors (pads and pours) of a net fall into connected groups over the items still alive."""
    seen: set[int] = set()
    out = []
    for a in sorted(anchors):
        if a in seen:
            continue
        comp, stack = {a}, [a]
        while stack:
            for j in adj[stack.pop()]:
                if j in alive and j not in comp:
                    comp.add(j)
                    stack.append(j)
        seen |= comp
        out.append(frozenset(comp & anchors))
    return frozenset(out)


def _bridge_clear(b: Board, routes: dict, fo: dict, net: int, layer: str, seg: tuple, w: float) -> bool:
    """A short joining segment must keep its clearance from every other net's pads, tracks and vias."""
    cl = b.clearance(net)
    for q in b.pads:
        if q.net != net and layer in q.layers:
            if _pad_seg_dist(q, *seg) < w / 2 + (max(cl, b.clearance(q.net)) if q.net > 0 else cl) - 1e-6:
                return False
    tracks = [(t.layer, t.x1, t.y1, t.x2, t.y2, t.width, t.net) for t in b.tracks if t.net != net]
    vias = [(v.x, v.y, v.size, v.net) for v in b.vias if v.net != net]
    for n, r in routes.items():
        if n != net:
            tracks += [(*s, n) for s in r.segments]
            vias += [(x, y, r.params[2], n) for (x, y) in r.vias]
    for n, (segs, vs) in fo.items():
        if n != net:
            tracks += [(*s, n) for s in segs]
    for (l, x1, y1, x2, y2, tw, n) in tracks:
        if l == layer:
            c = max(cl, b.clearance(n)) if n > 0 else cl
            if _seg_seg_cross(*seg, x1, y1, x2, y2) or _seg_seg_dist(seg, (x1, y1, x2, y2)) < w / 2 + tw / 2 + c - 1e-6:
                return False
    for (x, y, d, n) in vias:
        c = max(cl, b.clearance(n)) if n > 0 else cl
        if _seg_dist(x, y, *seg) < w / 2 + d / 2 + c - 1e-6:
            return False
    return True


def prune(routes: dict, fo: dict[int, tuple[list, list]], b: Board) -> tuple[int, int]:
    """Take back the fan-out copper the routing did not use. Returns (vias, stubs) removed.

    The fan-out gives every hemmed-in pad a stub and a via before anything is routed, and the router then
    uses each one as it sees fit: it changes layer at the via, or carries on from it on the same layer, or
    leaves the pad some other way. What it did not need is left as a hole in the board, or a spur with a
    hole on the end.

    For each stub of a net that routed completely, in this order:

    1. take the stub and its via out. If every pad and pour of the net is still joined as before, they go;
    2. else take only the via out, and it goes if the net still holds together;
    3. else a track on the stub's layer reaches the stub only through the via's copper. The via is replaced
       by a short segment from the stub's end to that track, if it keeps its clearances;
    4. else the via stays: it is doing a job.

    "Joined as before" is measured by copper overlap, and "still holds together" by how KiCad joins items
    (a track end on the other item), so nothing is removed that would leave an unconnected end behind.
    A net left incomplete keeps all of it: the escape is where the missing track will be drawn from.
    """
    gone_vias = gone_stubs = 0
    for net, (segs, vias) in fo.items():
        r = routes.get(net)
        if r is None or not r.complete or not segs:
            continue
        items: list[tuple] = [('p', p) for p in b.pads if p.net == net]
        items += [('z', z) for z in b.zones if z.net == net and not z.is_keepout]
        anchors = set(range(len(items)))
        at: dict[tuple, int] = {}
        for s in list(segs) + list(r.segments) + [(t.layer, t.x1, t.y1, t.x2, t.y2, t.width) for t in b.tracks if t.net == net]:
            if ('s', *s[:5]) not in at:
                at[('s', *s[:5])] = len(items)
                items.append(('s', *s))
        for (x, y, d) in [(v.x, v.y, v.size) for v in b.vias if v.net == net] + [(x, y, r.params[2]) for (x, y) in r.vias]:
            if ('v', x, y) not in at:
                at[('v', x, y)] = len(items)
                items.append(('v', x, y, d))
        n = len(items)
        strict: list[set] = [set() for _ in range(n)]
        loose: list[set] = [set() for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                if _touch(items[i], items[j], False):
                    loose[i].add(j)
                    loose[j].add(i)
                    if _touch(items[i], items[j], True):
                        strict[i].add(j)
                        strict[j].add(i)
        alive = set(range(n))
        want = _groups(loose, alive, anchors)
        keep_segs, drop_vias = [], set()
        for stub in list(segs):
            layer, x1, y1, x2, y2, w = stub
            si = at[('s', *stub[:5])]
            vi = at.get(('v', x2, y2)) if (x2, y2) in vias else None
            gone = {si} if vi is None else {si, vi}
            if _groups(strict, alive - gone, anchors) == want:
                alive -= gone
                gone_stubs += 1
                if vi is not None:
                    gone_vias += 1
                    drop_vias.add((x2, y2))
                continue
            keep_segs.append(stub)
            if vi is None:
                continue
            if _groups(strict, alive - {vi}, anchors) == want:
                alive.discard(vi)
                gone_vias += 1
                drop_vias.add((x2, y2))
                continue
            # step 3: join the stub's end to the tracks that met it only inside the via
            bridges = []
            for j in sorted(loose[vi] & alive):
                it = items[j]
                if it[0] != 's' or it[1] != layer or j == si or j in strict[si]:
                    continue
                d, tx, ty = min((math.hypot(ex - x2, ey - y2), ex, ey) for ex, ey in ((it[2], it[3]), (it[4], it[5])))
                if d > items[vi][3] / 2 + it[6] / 2:            # the track only passes: join it at its nearest point
                    dx, dy = it[4] - it[2], it[5] - it[3]
                    t = max(0.0, min(1.0, ((x2 - it[2]) * dx + (y2 - it[3]) * dy) / (dx * dx + dy * dy or 1.0)))
                    tx, ty = it[2] + t * dx, it[3] + t * dy
                if math.hypot(tx - x2, ty - y2) > 1e-6:
                    bridges.append((layer, x2, y2, tx, ty, w))
            if not bridges or not all(_bridge_clear(b, routes, fo, net, layer, br[1:5], w) for br in bridges):
                continue
            first = len(items)
            for br in bridges:
                it = ('s', *br)
                links = {j for j in range(len(items)) if _touch(items[j], it, True)}
                for j in links:
                    strict[j].add(len(items))
                items.append(it)
                strict.append(links)
                loose.append(set())
            added = set(range(first, len(items)))
            if _groups(strict, (alive - {vi}) | added, anchors) == want:
                alive = (alive - {vi}) | added
                keep_segs += bridges
                gone_vias += 1
                drop_vias.add((x2, y2))
            else:                                               # no good: forget the bridges again
                for j in added:
                    for k in strict[j]:
                        if k < first:
                            strict[k].discard(j)
                del items[first:], strict[first:], loose[first:]
        if len(keep_segs) == len(segs) and not drop_vias:
            continue
        # a track the router ran to a via that is now gone is a spur: trim it back, end by end
        routed = {at[('s', *s[:5])] for s in r.segments} - {at[('s', *s[:5])] for s in segs}
        removed = set(range(n)) - alive
        queue = [j for i in removed for j in loose[i] if j in routed]
        trimmed: set[int] = set()
        while queue:
            i = queue.pop()
            if i not in alive or i not in routed:
                continue
            it = items[i]
            if _end_held(items, i, it[2], it[3], alive) and _end_held(items, i, it[4], it[5], alive):
                continue
            alive.discard(i)
            trimmed.add(i)
            queue += [j for j in loose[i] if j in routed and j in alive]
        if trimmed and _groups(strict, alive, anchors) != want:
            alive |= trimmed                                    # cannot happen for a true dead end; be safe
            trimmed = set()
        if trimmed:
            cut = {items[i][1:6] for i in trimmed}
            r.segments[:] = [s for s in r.segments if s[:5] not in cut]
            gone_stubs += len(trimmed)
        lost = {s[:5] for s in segs} - {s[:5] for s in keep_segs}
        b.tracks[:] = [t for t in b.tracks if not (t.net == net and t.span is None and (t.layer, t.x1, t.y1, t.x2, t.y2) in lost)]
        b.vias[:] = [v for v in b.vias if not (v.net == net and v.span is None and (v.x, v.y) in drop_vias)]
        segs[:] = keep_segs
        vias[:] = [v for v in vias if v not in drop_vias]
    return gone_vias, gone_stubs


def merge(routes: dict, fo: dict[int, tuple[list, list]], b: Board, make_route) -> None:
    """Put the fan-out copper into the routes dict so the writer emits it (also for nets left unrouted)."""
    for net, (segs, vias) in fo.items():
        if not segs and not vias:
            continue
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
