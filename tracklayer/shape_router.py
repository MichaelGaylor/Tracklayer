"""The shape-based engine: gridless routing on exact geometry.

Every piece of copper that is not the net being routed becomes a polygon grown by the clearance
plus half the track width (shapes.py). A track centreline is legal wherever it stays outside
those polygons, so the shortest legal route runs from obstacle corner to obstacle corner on
straight lines: the classic visibility graph. The graph is never built in full; A* generates
the visible neighbours of a point on demand inside a window that grows if the search fails.

Vias live on a fine lattice of spots that are legal for the net's via size on every layer
(checked with the raster code at exact lattice points). Any number of copper layers, joined by
through vias.

Connectivity (which pads, tracks, vias and pours already belong together) and the via lattice
come from the grid engine's rasters, built with no quantisation margin.
"""
from __future__ import annotations

import heapq
import math
import time

import numpy as np

from .board import Board
from .geom import capsule_mask, cells_of, circle_mask
from .router import ALL, FREE, NetRoute, Options, RouteClass, Router, _seg_dist
from .shapes import (SLACK, Obstacles, capsule_polygon, circle_polygon, point_in_polygon, polygon_grown,
                     rrect_polygon)
from .smooth import rectify

INF = float('inf')


class ShapeRouter:
    def __init__(self, board: Board, nets: set[int], opts: Options):
        self.b = board
        self.o = opts
        self.nets = sorted(nets)
        # the grid helper: connectivity, labels, via lattice (exact at lattice points, so no margin)
        gopts = Options(**{**opts.__dict__, 'pitch': opts.via_pitch, 'margin': 0.0, 'verbose': False})
        self.gr = Router(board, nets, gopts)
        self.layers = self.gr.layers
        self.L = len(self.layers)
        self.lidx = self.gr.lidx
        self.g = self.gr.g
        self.routes: dict[int, NetRoute] = {}
        self.unrouted: list[int] = []
        self.log: list[str] = []
        self.obs: dict[tuple, list[Obstacles]] = {}      # class key -> per layer
        self.outline = sorted(board.outline, key=lambda p: -abs(_area(p)))
        self.bx0, self.by0, self.bx1, self.by1 = board.bbox()

    # ------------------------------------------------------------------ plumbing
    def say(self, *a):
        msg = ' '.join(str(x) for x in a)
        self.log.append(msg)
        if self.o.verbose:
            print(msg, flush=True)

    def net_params(self, net):
        return self.gr.net_params(net)

    def _ckey(self, c: RouteClass):
        return (round(c.width, 4), round(c.clearance, 4), round(c.via_d, 4))

    # ------------------------------------------------------------------ obstacles
    def _class_obstacles(self, c: RouteClass) -> list[Obstacles]:
        key = self._ckey(c)
        if key in self.obs:
            return self.obs[key]
        b = self.b
        obs = [Obstacles() for _ in range(self.L)]
        w = c.width
        for p in b.pads:
            if p.net == -3:   # unplated hole
                R = w / 2 + max(c.clearance, b.hole_clearance) + SLACK
                net = ALL
            else:
                R = w / 2 + max(c.clearance, self.gr.obstacle_clearance(p.net)) + SLACK
                net = p.net if p.net > 0 else ALL
            poly = rrect_polygon(p.x, p.y, p.w, p.h, p.rot, p.corner_radius(), R)
            for l in p.layers:
                if l in self.lidx:
                    obs[self.lidx[l]].add(poly, net)
        for t in b.tracks:
            if t.layer not in self.lidx:
                continue
            R = w / 2 + max(c.clearance, self.gr.obstacle_clearance(t.net)) + SLACK
            obs[self.lidx[t.layer]].add(capsule_polygon(t.x1, t.y1, t.x2, t.y2, t.width / 2 + R), t.net if t.net > 0 else ALL)
        for v in b.vias:
            R = w / 2 + max(c.clearance, self.gr.obstacle_clearance(v.net)) + SLACK
            for l in v.layers:
                if l in self.lidx:
                    obs[self.lidx[l]].add(circle_polygon(v.x, v.y, v.size / 2 + R), v.net if v.net > 0 else ALL)
        for z in b.zones:
            if z.is_keepout and z.keepout_tracks:
                if z.keepout_classes is not None and c.netclass not in z.keepout_classes:
                    continue
                for l in z.layers:
                    if l in self.lidx:
                        for poly in polygon_grown(z.polygon, w / 2 + SLACK):
                            obs[self.lidx[l]].add(poly, ALL)
        for z in self.gr.local_pours:
            R = w / 2 + max(c.clearance, self.gr.obstacle_clearance(z.net)) + SLACK
            for l in z.layers:
                if l in self.lidx:
                    for poly in polygon_grown(z.polygon, R):
                        obs[self.lidx[l]].add(poly, z.net)
        for layer, pts, gw in b.copper_graphics:
            if layer not in self.lidx:
                continue
            R = w / 2 + c.clearance + SLACK + gw / 2
            polys = [capsule_polygon(*pts[0], *pts[1], R)] if len(pts) == 2 else polygon_grown(pts, R)
            for poly in polys:
                obs[self.lidx[layer]].add(poly, ALL)
        # the board edge: capsules along every outline edge, on every layer
        Re = w / 2 + b.edge_clearance + SLACK
        for poly in self.outline:
            n = len(poly)
            for k in range(n):
                (ax, ay), (bx, by) = poly[k], poly[(k + 1) % n]
                cap = capsule_polygon(ax, ay, bx, by, Re)
                for li in range(self.L):
                    obs[li].add(cap, ALL)
        # routes already laid
        for r in self.routes.values():
            self._add_route_obstacles(r, c, obs)
        self.obs[key] = obs
        return obs

    def _add_route_obstacles(self, r: NetRoute, c: RouteClass, obs: list[Obstacles]):
        wn, cln, vdn, _ = self.net_params(r.net)
        R = max(c.clearance, cln) + SLACK + c.width / 2
        for layer, x1, y1, x2, y2, ws in r.segments:
            obs[self.lidx[layer]].add(capsule_polygon(x1, y1, x2, y2, ws / 2 + R), r.net)
        for x, y in r.vias:
            for li in range(self.L):
                obs[li].add(circle_polygon(x, y, vdn / 2 + R), r.net)

    def _paint_route(self, r: NetRoute):
        for key, obs in self.obs.items():
            c = self.gr.classes[key] if key in self.gr.classes else self._class_from_key(key)
            self._add_route_obstacles(r, c, obs)
        self.gr._paint_route(r)      # keeps the via lattice honest

    def _class_from_key(self, key):
        for c in self.gr.classes.values():
            if self._ckey(c) == key:
                return c
        raise KeyError(key)

    def _rebuild(self):
        self.obs.clear()
        self.gr.routes = dict(self.routes)
        self.gr._rebuild_routed()

    def _inside_board(self, x: float, y: float) -> bool:
        if not self.outline:
            return self.bx0 <= x <= self.bx1 and self.by0 <= y <= self.by1
        if not point_in_polygon(x, y, self.outline[0]):
            return False
        return not any(point_in_polygon(x, y, hole) for hole in self.outline[1:])

    # ------------------------------------------------------------------ points of a net
    def _net_points(self, net: int):
        """Connected components of the net as lists of (layer, x, y) points a track may end on."""
        gr, g = self.gr, self.g
        items, centres = gr._net_items(net)
        comps = gr._components(items)
        out = []
        for comp in comps:
            pts = set()
            pad_cells = [c for c in comp if c in centres]
            for (l, j, i) in pad_cells:
                pts.add((l,) + centres[(l, j, i)])
            others = [c for c in comp if c not in centres]
            # tracks, vias and pours: thin the cells to a 1 mm lattice (always keep a few)
            step = max(1, int(round(1.0 / g.pitch)))
            for (l, j, i) in others:
                if j % step == 0 and i % step == 0:
                    pts.add((l,) + g.to_xy(j, i))
            if not pts and others:
                for (l, j, i) in others[:8]:
                    pts.add((l,) + g.to_xy(j, i))
            out.append(sorted(pts))
        return out

    # ------------------------------------------------------------------ via lattice
    def _via_ok(self, net: int, c: RouteClass) -> np.ndarray:
        bad = ((c.vl != FREE) & (c.vl != net)).any(axis=0) | c.edge_v
        bad |= ((c.vr != FREE) & (c.vr != net)).any(axis=0)
        return ~bad

    # ------------------------------------------------------------------ pour crossing cost
    def _zone_factor(self, l: int, x: float, y: float, X: np.ndarray, Y: np.ndarray, net: int) -> np.ndarray:
        """1 + the mean pour-crossing weight along each candidate segment (another net's pour
        costs zone_cost per mm for a plane, zone_cost_local for a small pour), sampled every 0.5 mm."""
        g = self.gr.g
        zo, zw = self.gr.zone_owner[l], self.gr.zone_w[l]
        if not zw.any():
            return np.ones(len(X))
        n = 12
        t = (np.arange(n) + 0.5) / n
        sx = x + (X[:, None] - x) * t[None, :]
        sy = y + (Y[:, None] - y) * t[None, :]
        i = np.clip(np.rint((sx - g.x0) / g.pitch).astype(np.int64), 0, g.nx - 1)
        j = np.clip(np.rint((sy - g.y0) / g.pitch).astype(np.int64), 0, g.ny - 1)
        own = zo[j, i]
        w = np.where((own != FREE) & (own != net), zw[j, i], 0.0)
        return 1.0 + w.mean(axis=1)

    # ------------------------------------------------------------------ search
    def _route_net(self, net: int) -> NetRoute:
        w, cl, vd, vdr = self.net_params(net)
        c = self.gr.class_of[net]
        r = NetRoute(net)
        r.params = (w, cl, vd, vdr)
        r.via_layers = (self.layers[0], self.layers[-1])
        comps = self._net_points(net)
        if len(comps) < 2:
            return r
        obs = self._class_obstacles(c)
        via_ok = self._via_ok(net, c)
        neck_cls = self.gr.neck_of.get(net)
        neck = None
        name = self.b.net_name(net)
        comps.sort(key=len, reverse=True)
        tree = set(comps[0])
        others = comps[1:]
        labels = self.gr._labels
        while others:
            targets = {p for comp in others for p in comp}
            where = {(l, round(x, 4), round(y, 4)): i for i, comp in enumerate(others) for (l, x, y) in comp}
            use_obs, width = obs, w
            path = self._astar(net, c, obs, via_ok, tree, targets)
            if path is None and neck_cls is not None:
                # the full width cannot leave or reach a pad cleanly: try the narrower escape width
                if neck is None:
                    neck = (self._class_obstacles(neck_cls), self._via_ok(net, neck_cls))
                use_obs, width = neck[0], neck_cls.width
                path = self._astar(net, neck_cls, neck[0], neck[1], tree, targets)
            if path is None:
                what = sorted({labels.get((l,) + self.g.to_cell(x, y), '') for (l, x, y) in targets} - {''})[:6]
                self.say(f'  {name}: no path for {len(others)} remaining connection(s) ({", ".join(what) or "copper"})')
                r.complete = False
                break
            reached = others.pop(where[path[-1]])
            n_vias = len(r.vias)
            new_pts = self._emit(r, path, use_obs, net, width)
            for (vx_, vy_) in r.vias[n_vias:]:
                # no later via of this net may overlap this one (hole to hole)
                j0, i0, m = circle_mask(self.g, vx_, vy_, vd + cl)
                if m is not None:
                    win = via_ok[j0:j0 + m.shape[0], i0:i0 + m.shape[1]]
                    win[m[:win.shape[0], :win.shape[1]]] = False
                if neck is not None:
                    win2 = neck[1][j0:j0 + m.shape[0], i0:i0 + m.shape[1]]
                    win2[m[:win2.shape[0], :win2.shape[1]]] = False
            tree |= set(reached)
            tree |= new_pts
        return r

    def _astar(self, net, c, obs, via_ok, sources, targets):
        """A* over the implicit visibility graph. From each point the candidates are the nearest
        obstacle corners, the nearest legal via spots and the nearest targets on that layer."""
        L = self.L
        K_V, K_VIA, K_T = self.o.shape_corners, self.o.shape_vias, 24
        W = self.o.shape_weight
        lc = self.gr.layer_cost
        tx = np.array([p[1] for p in targets]); ty = np.array([p[2] for p in targets]); tl = np.array([p[0] for p in targets])
        tset = set(targets)
        via_cost = self.o.via_cost
        g = self.g

        def key(l, x, y):
            return (l, round(x, 4), round(y, 4))

        gscore = {}
        parent = {}
        heap = []
        for (l, x, y) in sources:
            if obs[l].contains(x, y, net) >= 0:
                continue
            k = key(l, x, y)
            gscore[k] = 0.0
            parent[k] = None
            heapq.heappush(heap, (W * float(np.hypot(tx - x, ty - y).min()), l, x, y))
        if not heap:
            return None
        closed = set()
        expansions = 0
        verts = [obs[l].vertices() for l in range(L)]
        pnets = [np.array(obs[l].poly_net) for l in range(L)]
        while heap:
            f, l, x, y = heapq.heappop(heap)
            k = key(l, x, y)
            if k in closed:
                continue
            closed.add(k)
            if (l, x, y) in tset:
                path = []
                while k is not None:
                    path.append(k)
                    k = parent[k]
                path.reverse()
                return path
            expansions += 1
            if expansions > self.o.shape_expansions:
                return None
            gc = gscore[k]
            cx, cy = [], []
            # targets on this layer, nearest first
            m = tl == l
            if m.any():
                ttx, tty = tx[m], ty[m]
                if len(ttx) > K_T:
                    sel = np.argsort(np.hypot(ttx - x, tty - y))[:K_T]
                    ttx, tty = ttx[sel], tty[sel]
                cx.append(ttx); cy.append(tty)
            # obstacle corners on this layer: not our own net's, and only tangent ones (the line from
            # here must leave both neighbouring edges on the same side, else the corner is no use)
            vx, vy, vp, vi_, pvx, pvy, nvx, nvy = verts[l]
            near = obs[l].vertices_near(x, y, 4 * K_V)
            if len(near):
                vx_, vy_ = vx[near], vy[near]
                dx, dy = vx_ - x, vy_ - y
                s1 = dx * (pvy[near] - y) - dy * (pvx[near] - x)
                s2 = dx * (nvy[near] - y) - dy * (nvx[near] - x)
                keep = (pnets[l][vp[near]] != net) & (s1 * s2 >= 0)
                dd = np.where(keep, np.hypot(dx, dy), np.inf)
                if len(dd) > K_V:
                    sel = np.argpartition(dd, K_V)[:K_V]
                else:
                    sel = np.arange(len(dd))
                sel = sel[np.isfinite(dd[sel])]
                cx.append(vx_[sel]); cy.append(vy_[sel])
            n_plain = sum(len(a) for a in cx)
            # via spots: the nearest legal lattice points, from a window that grows if it is empty
            if L > 1:
                for Rw in (3.0, 8.0, 20.0):
                    j0, i0 = g.to_cell(x - Rw, y - Rw); j1, i1 = g.to_cell(x + Rw, y + Rw)
                    j0, i0 = max(j0, 0), max(i0, 0); j1, i1 = min(j1 + 1, g.ny), min(i1 + 1, g.nx)
                    if j1 <= j0 or i1 <= i0:
                        continue
                    jj, ii = np.nonzero(via_ok[j0:j1, i0:i1])
                    if len(jj) >= K_VIA or Rw >= 20.0:
                        break
                if len(jj):
                    px = g.x0 + (ii + i0) * g.pitch; py = g.y0 + (jj + j0) * g.pitch
                    if len(px) > K_VIA:
                        sel = np.argpartition(np.hypot(px - x, py - y), K_VIA)[:K_VIA]
                        px, py = px[sel], py[sel]
                    cx.append(px); cy.append(py)
            if not cx:
                continue
            X = np.concatenate(cx); Y = np.concatenate(cy)
            same = (np.abs(X - x) < 1e-6) & (np.abs(Y - y) < 1e-6)
            dist = np.hypot(X - x, Y - y)
            # test in distance bands so near candidates are checked against few edges
            blocked = np.zeros(len(X), dtype=bool)
            order = np.argsort(dist)
            for band in np.array_split(order, max(1, min(4, len(order) // 40))):
                if len(band):
                    blocked[band] = obs[l].blocked_many(x, y, X[band], Y[band], net)
            blocked |= same
            zf = self._zone_factor(l, x, y, X, Y, net)
            for q in np.nonzero(~blocked)[0]:
                qx, qy = float(X[q]), float(Y[q])
                if q < n_plain:
                    nk = key(l, qx, qy)
                    if nk in closed:
                        continue
                    ng = gc + float(dist[q]) * lc[l] * float(zf[q])
                    if ng < gscore.get(nk, INF):
                        gscore[nk] = ng; parent[nk] = k
                        heapq.heappush(heap, (ng + W * float(np.hypot(tx - qx, ty - qy).min()), l, qx, qy))
                else:
                    # a via spot: first the track to it on this layer, then the layer change
                    hq = W * float(np.hypot(tx - qx, ty - qy).min())
                    here = key(l, qx, qy)
                    ng = gc + float(dist[q]) * lc[l] * float(zf[q])
                    if ng < gscore.get(here, INF):
                        gscore[here] = ng; parent[here] = k     # a waypoint for the path, not a state to expand
                    base = ng + via_cost
                    for l2 in range(L):
                        if l2 == l:
                            continue
                        nk = key(l2, qx, qy)
                        if nk in closed:
                            continue
                        if base < gscore.get(nk, INF):
                            gscore[nk] = base; parent[nk] = here
                            heapq.heappush(heap, (base + hq, l2, qx, qy))
        return None

    def _emit(self, r: NetRoute, path, obs, net, w) -> set:
        """Path of (layer, x, y) -> segments and vias, after pulling each run straight."""
        runs = []
        for (l, x, y) in path:
            if runs and runs[-1][0] == l:
                runs[-1][1].append((x, y))
            else:
                runs.append((l, [(x, y)]))
        new_pts = set()
        for k, (l, pts) in enumerate(runs):
            o = obs[l]
            def clear(x1, y1, x2, y2, _o=o):
                return not _o.blocked(x1, y1, x2, y2, net)
            pts = self._pull(pts, lambda a, b: clear(a[0], a[1], b[0], b[1]))
            if self.o.angles in ('45', '90'):
                pts = rectify(pts, self.o.angles, clear)
            layer = self.layers[l]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                    continue
                r.segments.append((layer, x1, y1, x2, y2, w))
                r.length += math.hypot(x2 - x1, y2 - y1)
                new_pts.add((l, x1, y1)); new_pts.add((l, x2, y2)); new_pts.add((l, (x1 + x2) / 2, (y1 + y2) / 2))
            if k < len(runs) - 1:
                x, y = pts[-1]
                r.vias.append((x, y))
                for l2 in range(self.L):
                    new_pts.add((l2, x, y))
        return new_pts

    @staticmethod
    def _pull(pts, clear):
        if len(pts) <= 2:
            return pts
        out = [pts[0]]
        i = 0
        n = len(pts)
        while i < n - 1:
            j = n - 1
            while j > i + 1 and not clear(pts[i], pts[j]):
                j -= 1
            out.append(pts[j])
            i = j
        return out

    # ------------------------------------------------------------------ passes
    def run(self):
        t0 = time.time()
        order = self.gr._order(self.nets)
        self.say(f'shape engine: {self.L} layer(s) {", ".join(self.layers)}, {len(self.gr.classes)} route class(es), '
                 f'via lattice {self.o.via_pitch} mm')
        for n in order:
            r = self._route_net(n)
            if r.segments or r.complete:
                self.routes[n] = r
                self._paint_route(r)
        failed = [n for n in self.nets if n not in self.routes or not self.routes[n].complete]
        self.say(f'  pass 1 done in {time.time() - t0:.1f} s: {len(failed)} incomplete')
        # rip-up and retry: free the area around a failed net, route it first, then put the others back
        for p in range(2, self.o.passes + 1):
            if not failed:
                break
            tp = time.time()
            before = len(failed)
            for n in list(failed):
                victims = self._blockers(n)
                saved = {v: self.routes[v] for v in victims if v in self.routes}
                for v in victims:
                    self.routes.pop(v, None)
                self.routes.pop(n, None)
                self._rebuild()
                r = self._route_net(n)
                if r.complete:
                    self.routes[n] = r
                    self._paint_route(r)
                    lost = []
                    for v in victims:
                        rv = self._route_net(v)
                        if rv.complete:
                            self.routes[v] = rv
                            self._paint_route(rv)
                        else:
                            lost.append(v)
                    if lost:   # no net gain: put things back as they were
                        self.routes.pop(n, None)
                        for v in lost:
                            self.routes.pop(v, None)
                        self.routes.update(saved)
                        self._rebuild()
                else:
                    self.routes.update(saved)
                    self._rebuild()
            failed = [n for n in self.nets if n not in self.routes or not self.routes[n].complete]
            self.say(f'  pass {p} done in {time.time() - tp:.1f} s: {len(failed)} incomplete')
            if len(failed) >= before:
                break
        self.unrouted = failed
        self.gr.routes = self.routes
        conflicts = self.gr._conflicts()
        if conflicts:
            self.say(f'  WARNING: {len(conflicts)} net(s) collide: ' + ', '.join(self.b.net_name(n) for n in conflicts))
        self.say(f'finished in {time.time() - t0:.1f} s: {len(self.nets) - len(self.unrouted)}/{len(self.nets)} nets routed, '
                 f'{sum(len(r.vias) for r in self.routes.values())} vias, '
                 f'{sum(r.length for r in self.routes.values()):.0f} mm of track')
        if self.unrouted:
            self.say('unrouted: ' + ', '.join(self.b.net_name(n) for n in self.unrouted))

    def _blockers(self, net: int) -> list[int]:
        """Routed nets with copper near the straight lines between this net's pads."""
        ps = [p for p in self.b.pads if p.net == net]
        if len(ps) < 2:
            return []
        out = set()
        for a, b in zip(ps, ps[1:]):
            for n, r in self.routes.items():
                if n == net or n in out:
                    continue
                for layer, x1, y1, x2, y2, ws in r.segments:
                    if _seg_dist(a.x, a.y, b.x, b.y, x1, y1, x2, y2) < 2.0:
                        out.add(n)
                        break
        return sorted(out)


def _area(poly) -> float:
    a = 0.0
    n = len(poly)
    for k in range(n):
        (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2
