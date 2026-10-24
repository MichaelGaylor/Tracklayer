import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tracklayer.board import load
from tracklayer.router import Options, Router, _seg_dist
from tracklayer.shape_router import ShapeRouter
from tracklayer.smooth import smooth_polyline
from tracklayer.writer import write_board

from boards import header_and_chip, two_resistors, with_pour


def _load(text: str):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 't.kicad_pcb')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)
    return load(p)


def _clearance_ok(board, routes, clearance=0.2):
    """Every new segment keeps `clearance` from every pad of another net and from other nets' segments."""
    segs = []
    for r in routes.values():
        for layer, x1, y1, x2, y2, w in r.segments:
            segs.append((r.net, layer, x1, y1, x2, y2, w / 2))
    for net, layer, x1, y1, x2, y2, hw in segs:
        for p in board.pads:
            if p.net == net or layer not in p.layers:
                continue
            if p.shape == 'circle':
                d = _seg_dist(x1, y1, x2, y2, p.x, p.y, p.x, p.y) - p.w / 2 - hw
            else:
                d = _seg_rect_dist(x1, y1, x2, y2, p) - hw
            assert d >= clearance - 1e-6, f'{board.net_name(net)} too close to {p.ref}.{p.number}: {d:.3f}'
        for net2, layer2, a1, b1, a2, b2, hw2 in segs:
            if net2 == net or layer2 != layer:
                continue
            d = _seg_dist(x1, y1, x2, y2, a1, b1, a2, b2) - hw - hw2
            assert d >= clearance - 1e-6, f'{board.net_name(net)} and {board.net_name(net2)} are {d:.3f} apart'


def _seg_rect_dist(x1, y1, x2, y2, p):
    """Distance from a segment to a pad outline (rounded corners included, polygon slightly outside)."""
    from tracklayer.shapes import rrect_polygon
    poly = rrect_polygon(p.x, p.y, p.w, p.h, p.rot, p.corner_radius(), 0.0)
    best = math.inf
    n = len(poly)
    for k in range(n):
        (ax, ay), (bx, by) = poly[k], poly[(k + 1) % n]
        best = min(best, _seg_dist(x1, y1, x2, y2, ax, ay, bx, by))
    return best


def _nets(board):
    return {c for c in board.nets if c > 0}


def test_parser_reads_pads_and_outline():
    b = _load(header_and_chip())
    assert b.copper_layers == ['F.Cu', 'B.Cu']
    assert len(b.pads) == 16
    assert b.bbox() == (0.0, 0.0, 36.0, 20.0)
    through = [p for p in b.pads if p.through]
    assert len(through) == 8 and through[0].layers == ['F.Cu', 'B.Cu']
    assert b.nets_by_name and len(_nets(b)) == 8


def test_rotated_footprint_pad_positions():
    b = _load(with_pour())
    c1 = {p.number: p for p in b.pads if p.ref == 'C1'}
    # rotated 90 deg counter-clockwise: local +x goes to -y on screen
    assert abs(c1['1'].x - 8) < 1e-9 and abs(c1['1'].y - 6) < 1e-9
    assert abs(c1['2'].x - 8) < 1e-9 and abs(c1['2'].y - 4) < 1e-9


def test_crossing_nets_route_clean():
    b = _load(two_resistors())
    r = Router(b, _nets(b), Options(verbose=False))
    r.run()
    assert not r.unrouted
    assert all(rt.complete for rt in r.routes.values())
    _clearance_ok(b, r.routes)
    # the crossing needs at least one via (or a long detour); the result must be short
    total = sum(rt.length for rt in r.routes.values())
    assert total < 40


def test_eight_scrambled_nets():
    b = _load(header_and_chip())
    r = Router(b, _nets(b), Options(verbose=False))
    r.run()
    assert not r.unrouted, [b.net_name(n) for n in r.unrouted]
    _clearance_ok(b, r.routes)


def test_pour_net_ends_inside_pour_and_signal_crosses():
    b = _load(with_pour())
    r = Router(b, _nets(b), Options(verbose=False))
    r.run()
    assert not r.unrouted
    gnd = b.net_code('GND')
    g = r.routes[gnd]
    # both GND pads are on F.Cu, the pour on B.Cu: each needs a via that lands inside the pour
    assert len(g.vias) >= 1
    for x, y in g.vias:
        assert 2 < x < 34 and 2 < y < 18
    _clearance_ok(b, r.routes)


def test_writer_round_trip():
    b = _load(two_resistors())
    r = Router(b, _nets(b), Options(verbose=False))
    r.run()
    out = os.path.join(tempfile.mkdtemp(), 'o.kicad_pcb')
    write_board(b, r.routes, None, out)
    b2 = load(out)
    assert len(b2.tracks) == sum(len(rt.segments) for rt in r.routes.values())
    assert len(b2.vias) == sum(len(rt.vias) for rt in r.routes.values())
    assert {b2.net_name(t.net) for t in b2.tracks} <= {'A', 'B'}
    # routing again finds nothing left to do
    r2 = Router(b2, _nets(b2), Options(verbose=False))
    r2.run()
    assert sum(len(rt.segments) for rt in r2.routes.values()) == 0


def test_replace_removes_old_tracks():
    b = _load(two_resistors())
    r = Router(b, _nets(b), Options(verbose=False))
    r.run()
    out = os.path.join(tempfile.mkdtemp(), 'o.kicad_pcb')
    write_board(b, r.routes, None, out)
    b2 = load(out)
    n_first = len(b2.tracks)
    doomed = [t for t in b2.tracks] + [v for v in b2.vias]
    b2.tracks, b2.vias = [], []
    r2 = Router(b2, _nets(b2), Options(verbose=False))
    r2.run()
    out2 = os.path.join(tempfile.mkdtemp(), 'o2.kicad_pcb')
    write_board(b2, r2.routes, doomed, out2)
    b3 = load(out2)
    assert len(b3.tracks) == sum(len(rt.segments) for rt in r2.routes.values())
    assert n_first > 0 and len(b3.tracks) <= n_first + 2


def test_smoothing_45():
    # a staircase of 0.2 mm steps: diagonal then horizontal
    pts = [(0, 0), (0.2, 0.2), (0.4, 0.4), (0.6, 0.4), (0.8, 0.4), (1.0, 0.4)]
    out = smooth_polyline(pts, lambda *a: True, '45', 0.2)
    assert out == [(0, 0), (0.4, 0.4), (1.0, 0.4)]
    out = smooth_polyline(pts, lambda *a: True, 'any', 0.2)
    assert out[0] == (0, 0) and out[-1] == (1.0, 0.4) and len(out) == 2


# ---------------------------------------------------------------- shape engine

def test_shape_crossing_nets():
    b = _load(two_resistors())
    r = ShapeRouter(b, _nets(b), Options(verbose=False))
    r.run()
    assert not r.unrouted
    _clearance_ok(b, r.routes)
    assert sum(rt.length for rt in r.routes.values()) < 40


def test_shape_eight_scrambled_nets():
    b = _load(header_and_chip())
    r = ShapeRouter(b, _nets(b), Options(verbose=False))
    r.run()
    assert not r.unrouted, [b.net_name(n) for n in r.unrouted]
    _clearance_ok(b, r.routes)


def test_shape_exact_clearance_is_tight():
    """The shape engine gets within a few hundredths of the clearance; the grid engine cannot."""
    b = _load(header_and_chip())
    r = ShapeRouter(b, _nets(b), Options(verbose=False))
    r.run()
    best = math.inf
    for rt in r.routes.values():
        for layer, x1, y1, x2, y2, w in rt.segments:
            for p in b.pads:
                if p.net == rt.net or layer not in p.layers:
                    continue
                if p.shape == 'circle':
                    d = _seg_dist(x1, y1, x2, y2, p.x, p.y, p.x, p.y) - p.w / 2 - w / 2
                else:
                    d = _seg_rect_dist(x1, y1, x2, y2, p) - w / 2
                best = min(best, d)
    assert 0.2 - 1e-6 <= best < 0.25


def test_shape_pour_net():
    b = _load(with_pour())
    r = ShapeRouter(b, _nets(b), Options(verbose=False))
    r.run()
    assert not r.unrouted
    g = r.routes[b.net_code('GND')]
    assert len(g.vias) >= 1
    for x, y in g.vias:
        assert 2 < x < 34 and 2 < y < 18
    _clearance_ok(b, r.routes)


def test_six_layers_both_engines():
    b = _load(header_and_chip(layers=6))
    assert b.copper_layers == ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']
    for engine in (ShapeRouter, Router):
        r = engine(b, _nets(b), Options(verbose=False))
        r.run()
        assert not r.unrouted
        _clearance_ok(b, r.routes)
        layers_used = {seg[0] for rt in r.routes.values() for seg in rt.segments}
        assert layers_used <= set(b.copper_layers)
        for rt in r.routes.values():
            assert rt.via_layers == ('F.Cu', 'B.Cu')
    out = os.path.join(tempfile.mkdtemp(), 'six.kicad_pcb')
    write_board(b, r.routes, None, out)
    b2 = load(out)
    assert len(b2.copper_layers) == 6 and len(b2.tracks) > 0
