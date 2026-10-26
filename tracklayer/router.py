"""The grid router.

Overview
--------
* The board is rasterised onto a square grid, one plane per copper layer.
* For every *route class* (track width, clearance, via size) there is an "owner" raster per layer:
  each cell holds the net whose copper, grown by the clearance a track of that class needs, covers
  the cell; a cell reached by two different nets holds ALL. A class-k track of net n may run through
  a cell iff the owner is FREE or n. Locked copper (pads, existing tracks, keep-outs) and copper the
  router has laid are kept in separate rasters so routes can be ripped up.
* Each net is routed as a Steiner-ish tree: its connected components (pads, existing copper, pours
  of its own net) are joined one at a time by multi-source A* on the grid, with 8 neighbours per
  layer and through vias between layers.
* Passes: pass 1 routes nets in order with other routes as hard obstacles. Later passes rip up the
  nets that failed or collided and reroute them with collisions allowed at a rising cost
  (negotiated congestion). Nets still colliding at the end are left unrouted and reported.
* Every finished route is smoothed into 45-degree or any-angle segments, checked against the same
  rasters, then painted back as an obstacle for the nets that follow.
"""
from __future__ import annotations

import copy
import heapq
import math
import time
from dataclasses import dataclass, field

import numpy as np

from .board import Board, Pad, Track, Via, Zone
from .geom import (Grid, add_mask, capsule_mask, cells_of, circle_mask, paint_owner, polygon_mask,
                   rrect_mask, set_mask)
from .smooth import smooth_polyline

FREE, ALL, NPTH = -1, -2, -3
INF = float('inf')
SQ2 = math.sqrt(2.0)


@dataclass
class Options:
    pitch: float = 0.2            # grid pitch, mm
    layers: list[str] | None = None
    layer_costs: dict | None = None   # {'F.Cu': 1.6}: a track on that layer counts 1.6 times its length
    passes: int = 8
    via_cost: float = 4.0         # mm of track a via is worth
    bend_cost: float = 0.3        # mm per direction change
    zone_cost: float = 0.3        # extra cost factor for running through another net's plane-sized pour
    zone_cost_local: float = 6.0  # ... and through a smaller pour, which a track would cut into islands
    prox_cost: float = 0.25       # extra cost factor for hugging copper
    dir_pref: float = 0.15        # extra cost for going against the layer's preferred direction
    conflict_cost: float = 3.0    # cost factor per pass for sharing a cell in negotiation passes
    history_cost: float = 1.0
    angles: str = '45'            # '45', 'any' or '90'
    route_zone_nets: bool = True  # nets with pours: route pads that lie outside the pour to it
    pour_obstacle_frac: float = 0.0   # pours smaller than this fraction of the board are hard obstacles for other nets
    pour_local_frac: float = 0.5      # pours smaller than this fraction of the board get zone_cost_local
    pour_depth: float = 1.0           # how far inside a pour a track of its own net must end, mm
    via_in_pad: bool = False
    margin: float | None = None   # grid quantisation margin added to every clearance; default 0.71 * pitch
    neck_width: float | None = None   # width used to escape tight pads; default: the Default class width
    width: float | None = None    # overrides for every routed net
    clearance: float | None = None
    via_diameter: float | None = None
    via_drill: float | None = None
    max_expansions: int = 4_000_000
    verbose: bool = True
    engine: str = 'shape'         # 'shape' (exact geometry) or 'grid'
    via_pitch: float = 0.25       # shape engine: lattice of candidate via positions, mm
    shape_corners: int = 160      # shape engine: nearest obstacle corners tried from each point
    shape_vias: int = 40          # shape engine: nearest via spots tried from each point
    shape_expansions: int = 10000 # shape engine: search budget per connection
    shape_weight: float = 1.25    # shape engine: heuristic weight (>1 trades a little length for speed)


@dataclass
class RouteClass:
    width: float
    clearance: float
    via_d: float
    via_drill: float
    netclass: str = ''      # the KiCad net class, set when the board has class-selective rule areas
    tl: np.ndarray = None   # track owner, locked copper   [L, ny, nx] int16
    vl: np.ndarray = None   # via owner, locked copper
    tr: np.ndarray = None   # track owner, routed copper
    vr: np.ndarray = None   # via owner, routed copper
    edge_t: np.ndarray = None   # [ny, nx] bool: a track centre may not be here (outline)
    edge_v: np.ndarray = None   # [ny, nx] bool: a via may not be here
    prox: np.ndarray = None     # [L, ny, nx] float32 proximity cost


@dataclass
class NetRoute:
    net: int
    segments: list[tuple[str, float, float, float, float, float]] = field(default_factory=list)  # layer, x1, y1, x2, y2, width
    vias: list[tuple[float, float]] = field(default_factory=list)
    cells: list[tuple[int, int, int]] = field(default_factory=list)      # (layer index, j, i) of the centrelines
    complete: bool = True
    length: float = 0.0
    params: tuple[float, float, float, float] = (0.25, 0.2, 0.8, 0.4)   # width, clearance, via size, via drill
    via_layers: tuple[str, str] | None = None


class Router:
    def __init__(self, board: Board, nets: set[int], opts: Options):
        self.b = board
        self.o = opts
        self.nets = sorted(nets)
        self.layers = opts.layers or list(board.copper_layers)
        self.L = len(self.layers)
        self.lidx = {l: k for k, l in enumerate(self.layers)}
        self.margin = opts.margin if opts.margin is not None else 0.71 * opts.pitch
        self.layer_cost = [max(1.0, float((opts.layer_costs or {}).get(l, 1.0))) for l in self.layers]
        x0, y0, x1, y1 = board.bbox()
        m = 3 * opts.pitch
        self.g = Grid(x0 - m, y0 - m, x1 + m, y1 + m, opts.pitch)
        self.NX, self.NY = self.g.nx, self.g.ny
        self.LN = self.NX * self.NY
        self.N = self.L * self.LN
        self.routes: dict[int, NetRoute] = {}
        self.unrouted: list[int] = []
        self.hist = np.zeros((self.L, self.NY, self.NX), dtype=np.float32)
        self.log = []
        self._build_static()

    # ------------------------------------------------------------------ setup
    def say(self, *a):
        msg = ' '.join(str(x) for x in a)
        self.log.append(msg)
        if self.o.verbose:
            print(msg, flush=True)

    def net_params(self, net: int) -> tuple[float, float, float, float]:
        nc = self.b.netclass(net)
        w = self.o.width or nc.track_width
        cl = self.o.clearance or max(nc.clearance, self.b.min_clearance)
        vd = self.o.via_diameter or nc.via_diameter
        vdr = self.o.via_drill or nc.via_drill
        return w, cl, vd, vdr

    def obstacle_clearance(self, net: int) -> float:
        if net < 0:
            return max(self.b.min_clearance, self.b.netclasses['Default'].clearance)
        return max(self.b.netclass(net).clearance, self.b.min_clearance)

    def _build_static(self):
        g, b, L = self.g, self.b, self.L
        t0 = time.time()
        # pour ownership and the cost of running another net's track through it
        bx0, by0, bx1, by1 = b.bbox()
        board_area = max((bx1 - bx0) * (by1 - by0), 1e-9)
        self.zone_owner = np.full((L, self.NY, self.NX), FREE, dtype=np.int16)
        self.zone_w = np.zeros((L, self.NY, self.NX), dtype=np.float32)
        for z in b.zones:
            if z.is_keepout or z.net <= 0:
                continue
            frac = abs(_area(z.polygon)) / board_area
            weight = self.o.zone_cost_local if frac < self.o.pour_local_frac else self.o.zone_cost
            j0, i0, m = polygon_mask(g, z.polygon)
            for l in z.layers:
                if l in self.lidx:
                    paint_owner(self.zone_owner[self.lidx[l]], j0, i0, m, z.net, ALL)
                    win = self.zone_w[self.lidx[l]][j0:j0 + m.shape[0], i0:i0 + m.shape[1]]
                    mm = m[:win.shape[0], :win.shape[1]]
                    win[mm] = np.maximum(win[mm], weight)
        # outline: inside mask (largest polygon is the board, the rest are cut-outs)
        self.inside = np.zeros((self.NY, self.NX), dtype=bool)
        polys = sorted(b.outline, key=lambda p: -abs(_area(p)))
        if polys:
            j0, i0, m = polygon_mask(g, polys[0])
            set_mask(self.inside, j0, i0, m, True)
            for hole in polys[1:]:
                j0, i0, m = polygon_mask(g, hole)
                set_mask(self.inside, j0, i0, m, False)
        else:
            self.inside[1:-1, 1:-1] = True
        self.inside[0, :] = self.inside[-1, :] = self.inside[:, 0] = self.inside[:, -1] = False

        # optionally, small pours are hard obstacles for other nets (only sane when no foreign pad sits in them)
        self.local_pours = [z for z in b.zones if not z.is_keepout and z.net > 0
                            and abs(_area(z.polygon)) / board_area < self.o.pour_obstacle_frac]
        # route classes
        self.classes: dict[tuple, RouteClass] = {}
        self.class_of: dict[int, RouteClass] = {}
        self.neck_of: dict[int, RouteClass | None] = {}
        neck_w = self.o.neck_width or b.netclasses['Default'].track_width
        selective = any(z.keepout_classes for z in self.b.zones)
        for n in self.nets:
            w, cl, vd, vdr = self.net_params(n)
            ncls = self.b.net_class_of.get(n, 'Default') if selective else ''
            key = (round(w, 4), round(cl, 4), round(vd, 4), ncls)
            if key not in self.classes:
                self.classes[key] = RouteClass(w, cl, vd, vdr, ncls)
            self.class_of[n] = self.classes[key]
            # a narrower class to escape pads that the full width cannot leave cleanly
            if neck_w < w - 1e-6:
                nkey = (round(neck_w, 4), round(cl, 4), round(vd, 4), ncls)
                if nkey not in self.classes:
                    self.classes[nkey] = RouteClass(neck_w, cl, vd, vdr, ncls)
                self.neck_of[n] = self.classes[nkey]
            else:
                self.neck_of[n] = None
        for c in self.classes.values():
            self._build_class(c)
        self.say(f'grid {self.NX}x{self.NY} cells x {L} layers at {self.o.pitch} mm, '
                 f'{len(self.classes)} route class(es), prepared in {time.time() - t0:.1f} s')

    def _pour_owner(self, z, layer: str) -> int:
        """The net of the pour a rule area sits on (same outline, or containing most of its vertices), else ALL."""
        from .shapes import point_in_polygon
        best, score = ALL, 0
        for z2 in self.b.zones:
            if z2.is_keepout or z2.net <= 0:
                continue
            if set(z2.polygon) == set(z.polygon):
                return z2.net
            n = sum(1 for (x, y) in z.polygon if point_in_polygon(x, y, z2.polygon))
            if n > score and n * 2 > len(z.polygon):
                best, score = z2.net, n
        return best

    def _edge_masks(self, r: float) -> np.ndarray:
        """Cells closer than r to the board edge (or outside)."""
        g = self.g
        near = ~self.inside
        for poly in self.b.outline:
            n = len(poly)
            for k in range(n):
                (ax, ay), (bx, by) = poly[k], poly[(k + 1) % n]
                j0, i0, m = capsule_mask(g, ax, ay, bx, by, r)
                set_mask(near, j0, i0, m, True)
        return near

    def _build_class(self, c: RouteClass):
        g, b, L = self.g, self.b, self.L
        shape = (L, self.NY, self.NX)
        c.tl = np.full(shape, FREE, dtype=np.int16)
        c.vl = np.full(shape, FREE, dtype=np.int16)
        c.tr = np.full(shape, FREE, dtype=np.int16)
        c.vr = np.full(shape, FREE, dtype=np.int16)
        c.edge_t = self._edge_masks(c.width / 2 + b.edge_clearance + self.margin)
        c.edge_v = self._edge_masks(c.via_d / 2 + b.edge_clearance + self.margin)
        mg = self.margin
        # pads
        for p in b.pads:
            if p.net == NPTH:
                rt = c.width / 2 + max(c.clearance, b.hole_clearance) + mg
                rv = c.via_d / 2 + max(c.clearance, b.hole_clearance) + mg
                net_t = net_v = ALL
            else:
                R = max(c.clearance, self.obstacle_clearance(p.net)) + mg
                rt, rv = c.width / 2 + R, c.via_d / 2 + R
                net_t = p.net if p.net > 0 else ALL
                net_v = net_t if (self.o.via_in_pad and not p.through) else ALL
            for l in p.layers:
                if l not in self.lidx:
                    continue
                li = self.lidx[l]
                j0, i0, m = rrect_mask(g, p.x, p.y, p.w, p.h, p.rot, p.corner_radius(), rt)
                paint_owner(c.tl[li], j0, i0, m, net_t, ALL)
                j0, i0, m = rrect_mask(g, p.x, p.y, p.w, p.h, p.rot, p.corner_radius(), rv)
                paint_owner(c.vl[li], j0, i0, m, net_v, ALL)
            if p.through and p.drill > 0:
                # the hole itself blocks vias on every layer
                for li in range(L):
                    j0, i0, m = circle_mask(g, p.x, p.y, p.drill / 2 + c.via_d / 2 + max(c.clearance, b.hole_clearance) + mg)
                    paint_owner(c.vl[li], j0, i0, m, ALL, ALL)
        # existing tracks and vias (locked copper)
        for t in b.tracks:
            if t.layer not in self.lidx:
                continue
            li = self.lidx[t.layer]
            R = max(c.clearance, self.obstacle_clearance(t.net)) + mg
            net = t.net if t.net > 0 else ALL
            j0, i0, m = capsule_mask(g, t.x1, t.y1, t.x2, t.y2, t.width / 2 + c.width / 2 + R)
            paint_owner(c.tl[li], j0, i0, m, net, ALL)
            j0, i0, m = capsule_mask(g, t.x1, t.y1, t.x2, t.y2, t.width / 2 + c.via_d / 2 + R)
            paint_owner(c.vl[li], j0, i0, m, net, ALL)
        for v in b.vias:
            R = max(c.clearance, self.obstacle_clearance(v.net)) + mg
            net = v.net if v.net > 0 else ALL
            for l in v.layers:
                if l not in self.lidx:
                    continue
                li = self.lidx[l]
                j0, i0, m = circle_mask(g, v.x, v.y, v.size / 2 + c.width / 2 + R)
                paint_owner(c.tl[li], j0, i0, m, net, ALL)
            for li in range(L):   # the hole blocks vias on every layer
                j0, i0, m = circle_mask(g, v.x, v.y, v.size / 2 + c.via_d / 2 + R)
                paint_owner(c.vl[li], j0, i0, m, ALL, ALL)
        for z in self.local_pours:
            R = max(c.clearance, self.obstacle_clearance(z.net)) + mg
            for l in z.layers:
                if l not in self.lidx:
                    continue
                li = self.lidx[l]
                j0, i0, m = polygon_mask(g, z.polygon, c.width / 2 + R)
                paint_owner(c.tl[li], j0, i0, m, z.net, ALL)
                j0, i0, m = polygon_mask(g, z.polygon, c.via_d / 2 + R)
                paint_owner(c.vl[li], j0, i0, m, z.net, ALL)
        # keep-outs and copper graphics
        for z in b.zones:
            if not z.is_keepout:
                continue
            if z.keepout_classes is not None and c.netclass not in z.keepout_classes:
                continue
            for l in z.layers:
                if l not in self.lidx:
                    continue
                li = self.lidx[l]
                if z.keepout_classes is not None:
                    # a class-selective area keeps the listed classes' vias out as well, so a net cannot drop in and
                    # leave on another layer; the net whose pour the area covers may still via into its own copper
                    j0, i0, m = polygon_mask(g, z.polygon, c.via_d / 2 + mg)
                    paint_owner(c.vl[li], j0, i0, m, self._pour_owner(z, l), ALL)
                if z.keepout_tracks:
                    j0, i0, m = polygon_mask(g, z.polygon, c.width / 2 + mg)
                    paint_owner(c.tl[li], j0, i0, m, ALL, ALL)
                if z.keepout_vias:
                    j0, i0, m = polygon_mask(g, z.polygon, c.via_d / 2 + mg)
                    paint_owner(c.vl[li], j0, i0, m, ALL, ALL)
        for layer, pts, w in b.copper_graphics:
            if layer not in self.lidx:
                continue
            li = self.lidx[layer]
            R = c.clearance + mg + w / 2
            if len(pts) == 2:
                j0, i0, m = capsule_mask(g, *pts[0], *pts[1], c.width / 2 + R)
                paint_owner(c.tl[li], j0, i0, m, ALL, ALL)
                j0, i0, m = capsule_mask(g, *pts[0], *pts[1], c.via_d / 2 + R)
                paint_owner(c.vl[li], j0, i0, m, ALL, ALL)
            else:
                j0, i0, m = polygon_mask(g, pts, c.width / 2 + R)
                paint_owner(c.tl[li], j0, i0, m, ALL, ALL)
                j0, i0, m = polygon_mask(g, pts, c.via_d / 2 + R)
                paint_owner(c.vl[li], j0, i0, m, ALL, ALL)
        # proximity cost: cells next to locked copper
        c.prox = np.zeros(shape, dtype=np.float32)
        occ = c.tl != FREE
        near = np.zeros_like(occ)
        near[:, 1:, :] |= occ[:, :-1, :]; near[:, :-1, :] |= occ[:, 1:, :]
        near[:, :, 1:] |= occ[:, :, :-1]; near[:, :, :-1] |= occ[:, :, 1:]
        c.prox[near & ~occ] = self.o.prox_cost

    # ------------------------------------------------------------------ painting routes
    def _paint_route(self, r: NetRoute):
        wn, cln, vdn, _ = self.net_params(r.net)
        for c in self.classes.values():
            R = max(c.clearance, cln) + self.margin
            for layer, x1, y1, x2, y2, ws in r.segments:
                li = self.lidx[layer]
                j0, i0, m = capsule_mask(self.g, x1, y1, x2, y2, c.width / 2 + R + ws / 2)
                paint_owner(c.tr[li], j0, i0, m, r.net, ALL)
                j0, i0, m = capsule_mask(self.g, x1, y1, x2, y2, c.via_d / 2 + R + ws / 2)
                paint_owner(c.vr[li], j0, i0, m, r.net, ALL)
            for x, y in r.vias:
                for li in range(self.L):
                    j0, i0, m = circle_mask(self.g, x, y, c.width / 2 + R + vdn / 2)
                    paint_owner(c.tr[li], j0, i0, m, r.net, ALL)
                    j0, i0, m = circle_mask(self.g, x, y, c.via_d / 2 + R + vdn / 2)
                    paint_owner(c.vr[li], j0, i0, m, ALL, ALL)   # a hole: no other via may overlap it

    def _rebuild_routed(self):
        for c in self.classes.values():
            c.tr.fill(FREE); c.vr.fill(FREE)
        for r in self.routes.values():
            self._paint_route(r)

    # ------------------------------------------------------------------ components of a net
    def _net_items(self, net: int):
        """Connected components of the net's existing copper as sets of (layer, j, i) cells that a
        new track may end on, plus the pad-centre lookup for tidy track ends."""
        g, b = self.g, self.b
        w = self.net_params(net)[0]
        neck = self.neck_of.get(net)
        w_src = neck.width if neck else w
        items: list[list[tuple[int, int, int]]] = []
        centres: dict[tuple[int, int, int], tuple[float, float]] = {}
        self._labels: dict[tuple[int, int, int], str] = {}
        for p in b.pads:
            if p.net != net:
                continue
            cells = []
            cj, ci = g.to_cell(p.x, p.y)
            for l in p.layers:
                if l not in self.lidx:
                    continue
                li = self.lidx[l]
                j0, i0, m = rrect_mask(g, p.x, p.y, p.w, p.h, p.rot, p.corner_radius(), -w_src / 2)
                cs = cells_of(j0, i0, m)
                if 0 <= cj < self.NY and 0 <= ci < self.NX:
                    cs.append((cj, ci))
                for j, i in set(cs):
                    cells.append((li, j, i))
                    centres[(li, j, i)] = (p.x, p.y)
                    self._labels[(li, j, i)] = f'{p.ref}.{p.number}'
            if cells:
                items.append(cells)
        for t in b.tracks:
            if t.net != net or t.layer not in self.lidx:
                continue
            li = self.lidx[t.layer]
            j0, i0, m = capsule_mask(g, t.x1, t.y1, t.x2, t.y2, max(t.width / 2 * 0.9, 0.01))
            cs = [(li, j, i) for j, i in cells_of(j0, i0, m)]
            if cs:
                items.append(cs)
        for v in b.vias:
            if v.net != net:
                continue
            cs = []
            for l in v.layers:
                if l in self.lidx:
                    j0, i0, m = circle_mask(g, v.x, v.y, max(v.size / 2 * 0.9, 0.01))
                    cs += [(self.lidx[l], j, i) for j, i in cells_of(j0, i0, m)]
            if cs:
                items.append(cs)
        if self.o.route_zone_nets:
            cl = self.net_params(net)[1]
            for z in b.zones:
                if z.is_keepout or z.net != net:
                    continue
                zl = [self.lidx[l] for l in z.layers if l in self.lidx]
                if not zl:
                    continue
                # the copper is the saved fill when the board has one, else the outline
                polys = [(self.lidx[l], pts) for l, pts in z.filled if l in self.lidx] or [(li, z.polygon) for li in zl]
                item = []
                inside = set()
                for li, pts in polys:
                    # a new track may end deep inside the pour, where the fill is sure to be
                    j0, i0, m = polygon_mask(g, pts, -(w / 2 + cl + self.margin + self.o.pour_depth))
                    item += [(li, j, i) for j, i in cells_of(j0, i0, m)]
                    j0, i0, m = polygon_mask(g, pts, 0.0)
                    inside |= {(li, j, i) for j, i in cells_of(j0, i0, m)}
                # the net's own pads, vias and tracks overlapping the fill are joined to it
                for it in items:
                    if any(c in inside for c in it):
                        item += [c for c in it if c[0] in zl]
                if item:
                    items.append(item)
        return items, centres

    def _components(self, items):
        parent = list(range(len(items)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        owner: dict[tuple[int, int, int], int] = {}
        for k, cells in enumerate(items):
            for c in cells:
                if c in owner:
                    parent[find(k)] = find(owner[c])
                else:
                    owner[c] = k
        # neighbouring cells on the same layer join
        for (l, j, i), k in owner.items():
            for dj, di in ((0, 1), (1, 0), (1, 1), (1, -1)):
                o = owner.get((l, j + dj, i + di))
                if o is not None and find(o) != find(k):
                    parent[find(o)] = find(k)
        comps: dict[int, set] = {}
        for c, k in owner.items():
            comps.setdefault(find(k), set()).add(c)
        return list(comps.values())

    # ------------------------------------------------------------------ per-net search data
    def _prepare(self, net: int, soft: bool, pen: float, c: RouteClass | None = None):
        c = c or self.class_of[net]
        hard = (c.tl != FREE) & (c.tl != net)
        hard |= c.edge_t[None, :, :]
        cost = c.prox.copy()
        cost += self.zone_w * ((self.zone_owner != FREE) & (self.zone_owner != net))
        cost += self.o.history_cost * self.hist
        other = (c.tr != FREE) & (c.tr != net)
        if soft:
            cost += pen * other
        else:
            hard |= other
        via_bad = ((c.vl != FREE) & (c.vl != net)).any(axis=0) | c.edge_v
        via_other = ((c.vr != FREE) & (c.vr != net)).any(axis=0)
        via_pen = np.zeros((self.NY, self.NX), dtype=np.float32)
        if soft:
            via_pen += pen * self.o.via_cost * via_other
        else:
            via_bad |= via_other
        return (bytearray(hard.astype(np.uint8).tobytes()), cost.ravel().tolist(),
                bytearray((~via_bad).astype(np.uint8).tobytes()), via_pen.ravel().tolist(), hard | other)

    # ------------------------------------------------------------------ A*
    def _astar(self, sources, target: bytearray, hard: bytearray, cost: list, via_ok: bytearray,
               via_pen: list, boxes: list):
        NX, LN, L, N = self.NX, self.LN, self.L, self.N
        pitch = self.o.pitch
        bend = self.o.bend_cost
        via_cost = self.o.via_cost
        dp = self.o.dir_pref
        g = [INF] * N
        parent = [-1] * N
        pdir = bytearray([255]) * N
        closed = bytearray(N)
        offs = ((-NX - 1, SQ2, 0), (-NX, 1.0, 1), (-NX + 1, SQ2, 0), (-1, 1.0, 2), (1, 1.0, 2),
                (NX - 1, SQ2, 0), (NX, 1.0, 1), (NX + 1, SQ2, 0))
        if self.o.angles == '90':
            offs = tuple(o for o in offs if o[1] == 1.0)
        # per-layer extra factor for vertical (axis 1) / horizontal (axis 2) moves
        pref = []
        lc = self.layer_cost
        for l in range(L):
            horiz = (l % 2 == 0)
            pref.append((0.0, dp if horiz else 0.0, 0.0 if horiz else dp))
        hmul = pitch
        # heuristic to the nearest target box, octile
        if len(boxes) <= 8:
            def h(idx):
                rem = idx % LN
                i = rem % NX; j = rem // NX
                best = INF
                for i0, j0, i1, j1 in boxes:
                    dx = i0 - i if i < i0 else (i - i1 if i > i1 else 0)
                    dy = j0 - j if j < j0 else (j - j1 if j > j1 else 0)
                    d = (dx + dy * 0.41421356) if dx > dy else (dy + dx * 0.41421356)
                    if d < best:
                        best = d
                return best * hmul
        else:
            hv = self._h_array(boxes)
            def h(idx):
                return hv[idx % LN]
        heap = []
        for s in sources:
            if hard[s]:
                continue
            g[s] = 0.0
            heap.append((h(s), s))
        heapq.heapify(heap)
        push, pop = heapq.heappush, heapq.heappop
        expansions = 0
        limit = self.o.max_expansions
        while heap:
            f, cur = pop(heap)
            if closed[cur]:
                continue
            closed[cur] = 1
            if target[cur]:
                path = []
                while cur != -1:
                    path.append(cur)
                    cur = parent[cur]
                path.reverse()
                return path, expansions
            expansions += 1
            if expansions > limit:
                return None, expansions
            gc = g[cur]
            pd = pdir[cur]
            l = cur // LN
            rem = cur - l * LN
            pl = pref[l]
            for k, (doff, ln, axis) in enumerate(offs):
                nb = cur + doff
                if hard[nb] or closed[nb]:
                    continue
                ng = gc + pitch * ln * lc[l] * (1.0 + cost[nb] + pl[axis]) + (bend if (pd != k and pd != 255) else 0.0)
                if ng < g[nb]:
                    g[nb] = ng
                    parent[nb] = cur
                    pdir[nb] = k
                    push(heap, (ng + h(nb), nb))
            if L > 1 and via_ok[rem]:
                for l2 in range(L):
                    if l2 == l:
                        continue
                    nb = l2 * LN + rem
                    if hard[nb] or closed[nb]:
                        continue
                    ng = gc + via_cost + via_pen[rem]
                    if ng < g[nb]:
                        g[nb] = ng
                        parent[nb] = cur
                        pdir[nb] = 254
                        push(heap, (ng + h(nb), nb))
        return None, expansions

    def _h_array(self, boxes):
        I, J = np.meshgrid(np.arange(self.NX), np.arange(self.NY))
        best = np.full((self.NY, self.NX), INF, dtype=np.float64)
        for i0, j0, i1, j1 in boxes:
            dx = np.maximum(np.maximum(i0 - I, I - i1), 0)
            dy = np.maximum(np.maximum(j0 - J, J - j1), 0)
            mx, mn = np.maximum(dx, dy), np.minimum(dx, dy)
            best = np.minimum(best, mx + 0.41421356 * mn)
        return (best * self.o.pitch).ravel().tolist()

    # ------------------------------------------------------------------ routing one net
    def _route_net(self, net: int, soft: bool, pen: float) -> NetRoute:
        g = self.g
        w, cl, vd, vdr = self.net_params(net)
        items, centres = self._net_items(net)
        comps = self._components(items)
        r = NetRoute(net)
        r.params = (w, cl, vd, vdr)
        r.via_layers = (self.layers[0], self.layers[-1])
        if len(comps) < 2:
            return r
        full = self._prepare(net, soft, pen)
        neck_cls = self.neck_of.get(net)
        neck = None
        comps.sort(key=len, reverse=True)
        tree = set(comps[0])
        others = comps[1:]
        name = self.b.nets.get(net, str(net))
        while others:
            hard, cost, via_ok, via_pen, strict = full
            width = w
            target = bytearray(self.N)
            boxes = []
            where: dict[int, int] = {}
            for k, comp in enumerate(others):
                js = [c[1] for c in comp]; is_ = [c[2] for c in comp]
                boxes.append((min(is_), min(js), max(is_), max(js)))
                for (l, j, i) in comp:
                    idx = l * self.LN + j * self.NX + i
                    target[idx] = 1
                    where[idx] = k
            sources = [l * self.LN + j * self.NX + i for (l, j, i) in tree]
            path, nexp = self._astar(sources, target, hard, cost, via_ok, via_pen, boxes)
            if path is None and neck_cls is not None:
                # the full width cannot leave or reach a pad cleanly: try the narrower escape width
                if neck is None:
                    neck = self._prepare(net, soft, pen, neck_cls)
                hard, cost, via_ok, via_pen, strict = neck
                width = neck_cls.width
                path, nexp2 = self._astar(sources, target, hard, cost, via_ok, via_pen, boxes)
                nexp += nexp2
            if path is None:
                what = sorted({self._labels.get(c, '') for comp in others for c in comp} - {''})[:6]
                self.say(f'  {name}: no path for {len(others)} remaining connection(s) '
                         f'({", ".join(what) or "pour"}) after {nexp} expansions')
                r.complete = False
                break
            k = where[path[-1]]
            reached = others.pop(k)
            cells = [(idx // self.LN, (idx % self.LN) // self.NX, idx % self.NX) for idx in path]
            new_cells = self._emit(r, cells, centres, strict, width)
            tree |= reached
            tree |= new_cells
            # the new copper is an obstacle for this net's own later branches only via clearance;
            # for the search it is a source, so nothing to mark in `hard`.
        return r

    def _emit(self, r: NetRoute, cells, centres, strict: np.ndarray, w: float) -> set:
        """Turn a cell path into smoothed segments and vias; return the cells on the new copper."""
        g = self.g
        runs = []   # (layer, [cells])
        for c in cells:
            if runs and runs[-1][0] == c[0]:
                runs[-1][1].append(c)
            else:
                runs.append((c[0], [c]))
        new_cells = set()
        for k, (li, run) in enumerate(runs):
            pts = [g.to_xy(j, i) for (_, j, i) in run]
            path_set = {(j, i) for (_, j, i) in run}
            layer = self.layers[li]
            # tidy ends: start/finish at the pad centre when the run begins/ends in a pad
            if k == 0 and run[0] in centres:
                pts.insert(0, centres[run[0]])
            if k == len(runs) - 1 and run[-1] in centres:
                pts.append(centres[run[-1]])
            strict_l = strict[li]

            def clear(x1, y1, x2, y2, _s=strict_l, _p=path_set):
                n = max(2, int(math.hypot(x2 - x1, y2 - y1) / (self.o.pitch / 3)) + 1)
                for t in range(n + 1):
                    f = t / n
                    j, i = g.to_cell(x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
                    if j < 0 or i < 0 or j >= self.NY or i >= self.NX:
                        return False
                    if _s[j, i] and (j, i) not in _p:
                        return False
                return True

            pts = smooth_polyline(pts, clear, self.o.angles, self.o.pitch)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
                    continue
                r.segments.append((layer, x1, y1, x2, y2, w))
                r.length += math.hypot(x2 - x1, y2 - y1)
                j0, i0, m = capsule_mask(g, x1, y1, x2, y2, max(w / 2 * 0.9, 0.01))
                for j, i in cells_of(j0, i0, m):
                    new_cells.add((li, j, i))
            # only cells the finished copper really covers may be targets for later branches;
            # smoothing moves the track off some of the raw path cells
            for (_, j, i) in run:
                r.cells.append((li, j, i))
            if k < len(runs) - 1:
                _, j, i = run[-1]
                x, y = g.to_xy(j, i)
                r.vias.append((x, y))
                for l2 in range(self.L):
                    new_cells.add((l2, j, i))
        return new_cells

    # ------------------------------------------------------------------ the passes
    def run(self):
        t0 = time.time()
        order = self._order(self.nets)
        to_route = list(order)
        prev_failed: set[int] | None = None
        best: tuple[int, dict] | None = None    # (incomplete count, routes) of the best collision-free state
        for p in range(1, self.o.passes + 1):
            soft = p > 1
            pen = self.o.conflict_cost * (2 ** (p - 1))   # negotiated congestion: the price doubles every pass
            if soft:
                for n in to_route:
                    self.routes.pop(n, None)
                self._rebuild_routed()
            self.say(f'pass {p}: routing {len(to_route)} net(s){" with negotiation" if soft else ""}')
            tp = time.time()
            for n in to_route:
                r = self._route_net(n, soft, pen)
                if r.segments or r.complete:
                    self.routes[n] = r
                    self._paint_route(r)
            conflicted = self._conflicts()
            failed = [n for n in self.nets if n not in self.routes or not self.routes[n].complete]
            self.say(f'  pass {p} done in {time.time() - tp:.1f} s: {len(failed)} incomplete, {len(conflicted)} in conflict')
            if not conflicted and (best is None or len(failed) < best[0]):
                best = (len(failed), copy.deepcopy(self.routes))
            nxt = sorted(set(failed) | set(conflicted))
            if not nxt:
                break
            if not conflicted and prev_failed == set(failed):
                self.say('  no progress on the remaining nets; stopping')
                break
            prev_failed = set(failed)
            to_route = self._order(nxt)
            if p == self.o.passes and conflicted:
                # out of passes: drop colliding nets, smallest first, until nothing collides ...
                pads = {}
                for pd in self.b.pads:
                    pads[pd.net] = pads.get(pd.net, 0) + 1
                for n in sorted(to_route, key=lambda n: (pads.get(n, 0), self.routes[n].length if n in self.routes else 0)):
                    if n in conflicted and n in self.routes:
                        self.routes.pop(n)
                        self._rebuild_routed()
                        conflicted = self._conflicts()
                        if not conflicted:
                            break
                # ... unless an earlier collision-free pass left fewer nets unrouted
                now = sum(1 for n in self.nets if n not in self.routes or not self.routes[n].complete)
                if best is not None and best[0] < now:
                    self.say(f'  keeping the result of the best earlier pass ({best[0]} incomplete instead of {now})')
                    self.routes = best[1]
                    self._rebuild_routed()
        self.unrouted = [n for n in self.nets if n not in self.routes or not self.routes[n].complete]
        self.say(f'finished in {time.time() - t0:.1f} s: {len(self.nets) - len(self.unrouted)}/{len(self.nets)} nets routed, '
                 f'{sum(len(r.vias) for r in self.routes.values())} vias, '
                 f'{sum(r.length for r in self.routes.values()):.0f} mm of track')
        if self.unrouted:
            self.say('unrouted: ' + ', '.join(self.b.nets.get(n, str(n)) for n in self.unrouted))

    def _conflicts(self) -> list[int]:
        """Nets whose laid copper is closer than the clearance to another routed net's copper.
        Checked on the real geometry (segments and vias), not on the quantised rasters."""
        cell = 2.0
        items: list[tuple[int, float, float, float, float, float]] = []   # net, x1, y1, x2, y2, half width
        buckets: dict[tuple[int, int], list[int]] = {}
        for n, r in self.routes.items():
            w, cl, vd, _ = r.params
            for layer, x1, y1, x2, y2, ws in r.segments:
                items.append((n, x1, y1, x2, y2, ws / 2, self.lidx[layer]))
            for x, y in r.vias:
                for li in range(self.L):
                    items.append((n, x, y, x, y, vd / 2, li))
        for k, (n, x1, y1, x2, y2, hw, li) in enumerate(items):
            for bx in range(int((min(x1, x2) - hw - 1) // cell), int((max(x1, x2) + hw + 1) // cell) + 1):
                for by in range(int((min(y1, y2) - hw - 1) // cell), int((max(y1, y2) + hw + 1) // cell) + 1):
                    buckets.setdefault((li, bx, by), []).append(k)
        bad: dict[int, list[tuple[int, int, int]]] = {}
        seen = set()
        for lst in buckets.values():
            for a in range(len(lst)):
                ka = lst[a]
                na, ax1, ay1, ax2, ay2, ahw, li = items[ka]
                for b in range(a + 1, len(lst)):
                    kb = lst[b]
                    nb_, bx1, by1, bx2, by2, bhw, _ = items[kb]
                    if na == nb_ or (ka, kb) in seen:
                        continue
                    seen.add((ka, kb))
                    need = ahw + bhw + max(self.net_params(na)[1], self.net_params(nb_)[1]) - 1e-4
                    dist = _seg_dist(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)
                    if dist < need:
                        if self.o.verbose and len(bad) < 6:
                            self.say(f'  collision: {self.b.net_name(na)} / {self.b.net_name(nb_)} gap '
                                     f'{dist - ahw - bhw:.3f} at ({ax1:.2f},{ay1:.2f})-({ax2:.2f},{ay2:.2f}) vs ({bx1:.2f},{by1:.2f})-({bx2:.2f},{by2:.2f}) {self.layers[li]}')
                        mx, my = (ax1 + ax2 + bx1 + bx2) / 4, (ay1 + ay2 + by1 + by2) / 4
                        j, i = self.g.to_cell(mx, my)
                        bad.setdefault(na, []).append((li, j, i))
                        bad.setdefault(nb_, []).append((li, j, i))
        for n, cells in bad.items():
            for (li, j, i) in cells:
                j0, j1 = max(0, j - 3), min(self.NY, j + 4)
                i0, i1 = max(0, i - 3), min(self.NX, i + 4)
                self.hist[li, j0:j1, i0:i1] += 1.0
        return sorted(bad)

    def _order(self, nets) -> list[int]:
        """Fat and wide-clearance nets first (they have the fewest options), then short before long."""
        def key(n):
            w, cl, vd, _ = self.net_params(n)
            ps = [p for p in self.b.pads if p.net == n]
            if len(ps) < 2:
                return (-(w + cl), 1, 0.0)
            xs = [p.x for p in ps]; ys = [p.y for p in ps]
            return (-(w + cl), 0, (max(xs) - min(xs)) + (max(ys) - min(ys)) + 0.5 * len(ps))
        return sorted(nets, key=key)


def _seg_dist(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2) -> float:
    """Shortest distance between two segments."""
    def pt_seg(px, py, x1, y1, x2, y2):
        vx, vy = x2 - x1, y2 - y1
        L2 = vx * vx + vy * vy
        if L2 < 1e-12:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L2))
        return math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))
    # crossing?
    def orient(ax, ay, bx, by, cx, cy):
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    o1 = orient(ax1, ay1, ax2, ay2, bx1, by1); o2 = orient(ax1, ay1, ax2, ay2, bx2, by2)
    o3 = orient(bx1, by1, bx2, by2, ax1, ay1); o4 = orient(bx1, by1, bx2, by2, ax2, ay2)
    if (o1 * o2 < 0) and (o3 * o4 < 0):
        return 0.0
    return min(pt_seg(ax1, ay1, bx1, by1, bx2, by2), pt_seg(ax2, ay2, bx1, by1, bx2, by2),
               pt_seg(bx1, by1, ax1, ay1, ax2, ay2), pt_seg(bx2, by2, ax1, ay1, ax2, ay2))


def _area(poly) -> float:
    a = 0.0
    n = len(poly)
    for k in range(n):
        (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2
