"""Board model read from a .kicad_pcb (and its .kicad_pro for net classes).

Everything is in millimetres in KiCad's board frame (y grows downwards). Rotations are KiCad's:
degrees, counter-clockwise as seen on screen.
"""
from __future__ import annotations

import fnmatch
import re
import json
import math
import os
from dataclasses import dataclass, field

from .sexp import Sexp, num, parse

Pt = tuple[float, float]


def rot_local(px: float, py: float, deg: float) -> Pt:
    """Footprint-local -> board offset for a footprint rotated `deg` (KiCad convention)."""
    if not deg:
        return px, py
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return px * c + py * s, -px * s + py * c


@dataclass
class Pad:
    ref: str
    number: str
    net: int
    x: float
    y: float
    rot: float            # absolute orientation, degrees
    w: float              # size along the pad's own x before rotation
    h: float
    shape: str            # rect roundrect circle oval trapezoid custom
    rratio: float         # roundrect corner ratio of min(w, h)
    layers: list[str]     # copper layers the pad has copper on
    through: bool
    drill: float = 0.0

    def corner_radius(self) -> float:
        if self.shape == 'circle':
            return min(self.w, self.h) / 2
        if self.shape == 'oval':
            return min(self.w, self.h) / 2
        if self.shape == 'roundrect':
            return self.rratio * min(self.w, self.h)
        return 0.0


@dataclass
class Track:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: int
    locked: bool = False
    span: tuple[int, int] | None = None   # character span in the source file


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    net: int
    layers: list[str]
    locked: bool = False
    span: tuple[int, int] | None = None


_KEEPOUT_CLASSES = re.compile(r'keep-?out\s+class(?:es)?\s*:\s*([^;()]+)', re.IGNORECASE)


@dataclass
class Zone:
    net: int
    layers: list[str]
    polygon: list[Pt]
    priority: int = 0
    keepout_tracks: bool = False
    keepout_vias: bool = False
    is_keepout: bool = False
    keepout_classes: list[str] | None = None   # rule area named 'keepout classes: A, B': only these net classes stay out
    filled: list[tuple[str, list[Pt]]] = field(default_factory=list)   # (layer, pts) fill as last saved by KiCad


@dataclass
class NetClass:
    name: str
    clearance: float = 0.2
    track_width: float = 0.25
    via_diameter: float = 0.8
    via_drill: float = 0.4


@dataclass
class Board:
    path: str
    copper_layers: list[str] = field(default_factory=list)   # in stack order, F.Cu first
    nets: dict[int, str] = field(default_factory=dict)
    pads: list[Pad] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    copper_graphics: list[tuple[str, list[Pt], float]] = field(default_factory=list)  # (layer, pts, width) lines/polys on copper
    outline: list[list[Pt]] = field(default_factory=list)    # closed polygons on Edge.Cuts
    netclasses: dict[str, NetClass] = field(default_factory=dict)
    net_class_of: dict[int, str] = field(default_factory=dict)
    edge_clearance: float = 0.5
    hole_clearance: float = 0.25
    min_clearance: float = 0.0
    text: str = ''
    nets_by_name: bool = False   # KiCad 10 files name the net on each item instead of using codes

    def net_ref(self, node) -> int:
        """Net code for a `(net ...)` child; names are assigned codes as they are met."""
        n = node.find('net') if node is not None else None
        if n is None:
            return 0
        a = n.atoms()
        if not a:
            return 0
        if not self.nets_by_name and len(a) >= 1:
            try:
                return int(a[0])
            except ValueError:
                pass
        name = a[0]
        if name == '':
            return 0
        code = self.net_code(name)
        if code is None:
            code = max(self.nets, default=0) + 1
            self.nets[code] = name
        return code

    # ---- lookups ----
    def net_code(self, name: str) -> int | None:
        for code, n in self.nets.items():
            if n == name:
                return code
        return None

    def net_name(self, code: int) -> str:
        return self.nets.get(code, '')

    def netclass(self, net: int) -> NetClass:
        return self.netclasses.get(self.net_class_of.get(net, 'Default')) or self.netclasses.get('Default') or NetClass('Default')

    def clearance(self, net: int) -> float:
        return max(self.netclass(net).clearance, self.min_clearance)

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for poly in self.outline for p in poly]
        ys = [p[1] for poly in self.outline for p in poly]
        if not xs:
            xs = [p.x for p in self.pads]
            ys = [p.y for p in self.pads]
            if not xs:
                return 0.0, 0.0, 100.0, 100.0
            m = 5.0
            return min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m
        return min(xs), min(ys), max(xs), max(ys)


# ----------------------------------------------------------------------------------------------
# reading

def _xy(node: Sexp | None, default=(0.0, 0.0)) -> Pt:
    if node is None:
        return default
    a = node.atoms()
    return num(a[0]), num(a[1])


def _pts(node: Sexp | None) -> list[Pt]:
    if node is None:
        return []
    pts = node.find('pts')
    if pts is None:
        return []
    return [(num(p[1]), num(p[2])) for p in pts.find_all('xy')]


def _arc_points(p0: Pt, pm: Pt, p1: Pt, n: int = 16) -> list[Pt]:
    """Points along the circular arc through p0, pm, p1 (KiCad start/mid/end)."""
    ax, ay = p0; bx, by = pm; cx, cy = p1
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [p0, p1]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux); am = math.atan2(by - uy, bx - ux); a1 = math.atan2(cy - uy, cx - ux)
    # choose the sweep direction that passes through the mid point
    def sweep(s, e, ccw):
        d = (e - s) % (2 * math.pi)
        return d if ccw else d - 2 * math.pi
    for ccw in (True, False):
        total = sweep(a0, a1, ccw); mid = sweep(a0, am, ccw)
        if (0 <= mid <= total) if ccw else (total <= mid <= 0):
            break
    return [(ux + r * math.cos(a0 + total * k / n), uy + r * math.sin(a0 + total * k / n)) for k in range(n + 1)]


def _via_span(copper: list[str], named: list[str]) -> list[str]:
    """All copper layers a via passes through, given the layers the file names for it."""
    idx = [copper.index(l) for l in named if l in copper]
    if not idx:
        return [l for l in named]
    return copper[min(idx):max(idx) + 1]


def _chain(segments: list[tuple[Pt, Pt]], tol: float = 0.01) -> list[list[Pt]]:
    """Join loose edge segments into closed polygons."""
    segs = [list(s) for s in segments]
    polys = []
    while segs:
        poly = list(segs.pop(0))
        changed = True
        while changed:
            changed = False
            for k, s in enumerate(segs):
                a, b = s[0], s[-1]
                e = poly[-1]
                if math.dist(e, a) < tol:
                    poly += s[1:]; segs.pop(k); changed = True; break
                if math.dist(e, b) < tol:
                    poly += s[-2::-1]; segs.pop(k); changed = True; break
                st = poly[0]
                if math.dist(st, b) < tol:
                    poly = s[:-1] + poly; segs.pop(k); changed = True; break
                if math.dist(st, a) < tol:
                    poly = s[::-1][:-1] + poly; segs.pop(k); changed = True; break
        if len(poly) >= 3:
            if math.dist(poly[0], poly[-1]) < tol:
                poly = poly[:-1]
            polys.append(poly)
    return polys


def _pad(b: 'Board', fp_ref: str, fx: float, fy: float, frot: float, node: Sexp, copper: list[str]) -> Pad | None:
    a = node.atoms()
    number, kind, shape = a[0], a[1], a[2]
    at = node.find('at'); aa = at.atoms() if at is not None else ['0', '0']
    px, py = num(aa[0]), num(aa[1])
    prot = num(aa[2]) if len(aa) > 2 else 0.0
    ox, oy = rot_local(px, py, frot)
    sz = _xy(node.find('size'), (0.0, 0.0))
    layers_spec = node.values('layers')
    lay = set()
    for l in layers_spec:
        if l.endswith('.Cu'):
            if l == '*.Cu':
                lay.update(copper)
            else:
                lay.add(l)
        elif l in ('F&B.Cu',):
            lay.update([copper[0], copper[-1]])
    lay = [l for l in copper if l in lay]
    drill = 0.0
    dn = node.find('drill')
    if dn is not None:
        da = [x for x in dn.atoms() if x != 'oval']
        drill = max((num(x) for x in da), default=0.0)
    net = b.net_ref(node)
    through = kind in ('thru_hole', 'np_thru_hole')
    if kind == 'np_thru_hole':
        net = -3   # unplated hole: an obstacle for everyone (router.NPTH)
        if not lay:
            lay = list(copper)
    if not lay:
        return None
    return Pad(fp_ref, number, net, fx + ox, fy + oy, prot, sz[0], sz[1], shape,
               num(node.value('roundrect_rratio', '0.25')) if shape == 'roundrect' else 0.0,
               lay, through, drill)


def load(path: str, pro_path: str | None = None) -> Board:
    text = open(path, encoding='utf-8').read()
    root = parse(text)
    b = Board(path=path, text=text)

    layers_n = root.find('layers')
    if layers_n is not None:
        for l in layers_n:
            if isinstance(l, Sexp) and len(l) >= 3 and str(l[1]).endswith('.Cu') and l[2] in ('signal', 'power', 'mixed', 'jumper'):
                b.copper_layers.append(l[1])
    if not b.copper_layers:
        b.copper_layers = ['F.Cu', 'B.Cu']

    for n in root.find_all('net'):
        a = n.atoms()
        b.nets[int(num(a[0]))] = a[1] if len(a) > 1 else ''
    b.nets_by_name = not b.nets
    if b.nets_by_name:
        b.nets[0] = ''

    setup = root.find('setup')
    if setup is not None:
        pass

    edge_segs: list[tuple[Pt, Pt]] = []
    for g in root:
        if not isinstance(g, Sexp):
            continue
        k = g.key
        if k == 'footprint':
            at = g.find('at'); aa = at.atoms() if at is not None else ['0', '0']
            fx, fy = num(aa[0]), num(aa[1]); frot = num(aa[2]) if len(aa) > 2 else 0.0
            ref = ''
            for prop in g.find_all('property'):
                pa = prop.atoms()
                if pa and pa[0] == 'Reference' and len(pa) > 1:
                    ref = pa[1]
            if not ref:
                for t in g.find_all('fp_text'):
                    ta = t.atoms()
                    if ta and ta[0] == 'reference' and len(ta) > 1:
                        ref = ta[1]
            for p in g.find_all('pad'):
                pad = _pad(b, ref, fx, fy, frot, p, b.copper_layers)
                if pad is not None:
                    b.pads.append(pad)
        elif k == 'segment':
            t = Track(*_xy(g.find('start')), *_xy(g.find('end')), num(g.value('width', '0.25')),
                      g.value('layer', 'F.Cu'), b.net_ref(g), g.value('locked') == 'yes')
            t.span = g.span
            b.tracks.append(t)
        elif k == 'arc':
            pts = _arc_points(_xy(g.find('start')), _xy(g.find('mid')), _xy(g.find('end')))
            for p, q in zip(pts, pts[1:]):
                b.tracks.append(Track(*p, *q, num(g.value('width', '0.25')), g.value('layer', 'F.Cu'),
                                      b.net_ref(g), g.value('locked') == 'yes'))
        elif k == 'via':
            lay = g.values('layers') or [b.copper_layers[0], b.copper_layers[-1]]
            # a via drills through every copper layer between the two it names
            lay = _via_span(b.copper_layers, list(lay))
            v = Via(*_xy(g.find('at')), num(g.value('size', '0.8')), num(g.value('drill', '0.4')),
                    b.net_ref(g), lay, g.value('locked') == 'yes')
            v.span = g.span
            b.vias.append(v)
        elif k == 'zone':
            lay = g.values('layers') or [g.value('layer', 'F.Cu')]
            if '*.Cu' in lay or 'F&B.Cu' in lay:
                lay = list(b.copper_layers) if '*.Cu' in lay else [b.copper_layers[0], b.copper_layers[-1]]
            lay = [l for l in lay if l.endswith('.Cu')]
            ko = g.find('keepout')
            z = Zone(b.net_ref(g), lay, _pts(g.find('polygon')), int(num(g.value('priority', '0'))))
            if ko is not None:
                z.is_keepout = True
                z.keepout_tracks = ko.value('tracks') == 'not_allowed'
                z.keepout_vias = ko.value('vias') == 'not_allowed'
            m = _KEEPOUT_CLASSES.search(g.value('name', '') or '')
            if m:
                # class-selective rule area: tracks of the named classes keep out even when KiCad's own
                # restriction is off (so a custom DRC rule can enforce the same classes in KiCad)
                z.is_keepout = True
                z.keepout_tracks = True
                z.keepout_classes = [c for c in re.split(r'[,\s]+', m.group(1).strip()) if c]
            for fpoly in g.find_all('filled_polygon'):
                fl = fpoly.value('layer', lay[0] if lay else 'F.Cu')
                fp = _pts(fpoly)
                if len(fp) >= 3:
                    z.filled.append((fl, fp))
            if z.polygon:
                b.zones.append(z)
        elif k in ('gr_line', 'gr_rect', 'gr_arc', 'gr_circle', 'gr_poly'):
            layer = g.value('layer', '')
            if layer == 'Edge.Cuts':
                if k == 'gr_line':
                    edge_segs.append((_xy(g.find('start')), _xy(g.find('end'))))
                elif k == 'gr_rect':
                    (x1, y1), (x2, y2) = _xy(g.find('start')), _xy(g.find('end'))
                    b.outline.append([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])
                elif k == 'gr_arc':
                    pts = _arc_points(_xy(g.find('start')), _xy(g.find('mid')), _xy(g.find('end')))
                    edge_segs.append(tuple(pts))  # type: ignore[arg-type]
                elif k == 'gr_circle':
                    (cx, cy), (ex, ey) = _xy(g.find('center')), _xy(g.find('end'))
                    r = math.hypot(ex - cx, ey - cy)
                    b.outline.append([(cx + r * math.cos(t), cy + r * math.sin(t)) for t in [2 * math.pi * i / 48 for i in range(48)]])
                elif k == 'gr_poly':
                    b.outline.append(_pts(g))
            elif layer.endswith('.Cu'):
                w = num(g.find('stroke').value('width', '0') if g.find('stroke') is not None else g.value('width', '0'))
                if k == 'gr_line':
                    b.copper_graphics.append((layer, [_xy(g.find('start')), _xy(g.find('end'))], w))
                elif k == 'gr_poly':
                    b.copper_graphics.append((layer, _pts(g), w))
                elif k == 'gr_rect':
                    (x1, y1), (x2, y2) = _xy(g.find('start')), _xy(g.find('end'))
                    b.copper_graphics.append((layer, [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], w))
    if edge_segs:
        b.outline += _chain(edge_segs)

    # net classes from the project file
    if pro_path is None:
        cand = os.path.splitext(path)[0] + '.kicad_pro'
        pro_path = cand if os.path.exists(cand) else None
    b.netclasses['Default'] = NetClass('Default')
    if pro_path:
        _read_project(b, pro_path)
    return b


def _read_project(b: Board, pro_path: str) -> None:
    try:
        pro = json.load(open(pro_path, encoding='utf-8'))
    except (OSError, ValueError):
        return
    rules = pro.get('board', {}).get('design_settings', {}).get('rules', {})
    b.min_clearance = float(rules.get('min_clearance', 0.0) or 0.0)
    b.edge_clearance = float(rules.get('min_copper_edge_clearance', b.edge_clearance) or b.edge_clearance)
    b.hole_clearance = float(rules.get('min_hole_clearance', b.hole_clearance) or b.hole_clearance)
    ns = pro.get('net_settings', {})
    for c in ns.get('classes') or []:
        nc = NetClass(c.get('name', 'Default'))
        nc.clearance = float(c.get('clearance', nc.clearance))
        nc.track_width = float(c.get('track_width', nc.track_width))
        nc.via_diameter = float(c.get('via_diameter', nc.via_diameter))
        nc.via_drill = float(c.get('via_drill', nc.via_drill))
        b.netclasses[nc.name] = nc
    patterns = [(p.get('pattern', ''), p.get('netclass', 'Default')) for p in (ns.get('netclass_patterns') or [])]
    assignments = ns.get('netclass_assignments') or {}
    for code, name in b.nets.items():
        if name in assignments:
            b.net_class_of[code] = assignments[name] if isinstance(assignments[name], str) else assignments[name][0]
            continue
        for pat, cls in patterns:
            if fnmatch.fnmatchcase(name, pat):
                b.net_class_of[code] = cls
                break


def match_nets(b: Board, patterns: list[str]) -> set[int]:
    out = set()
    for code, name in b.nets.items():
        if code and any(fnmatch.fnmatchcase(name, p) for p in patterns):
            out.add(code)
    return out
