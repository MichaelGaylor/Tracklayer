import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from tracklayer.fanout import fanout
from tracklayer.router import Options, Router
from tracklayer.shape_router import ShapeRouter

from boards import board, footprint
from test_router import _load, _nets


def _soic_board():
    """An 8-pin SOIC (1.27 mm pitch, 0.6 x 1.5 pads) whose pins all have to reach a header 20 mm away."""
    smd = ('smd', 'rect', ['F.Cu'])
    pads = []
    for i in range(4):
        pads.append((str(i + 1), *smd[:2], -2.7, -1.905 + i * 1.27, 1.5, 0.6, smd[2], f'N{i + 1}'))
        pads.append((str(i + 5), *smd[:2], 2.7, 1.905 - i * 1.27, 1.5, 0.6, smd[2], f'N{i + 5}'))
    u = footprint('U1', 10, 10, 0, pads)
    hp = [(str(i + 1), 'thru_hole', 'circle', 0, (i - 3.5) * 2.54, 1.7, 1.7, ['F.Cu', 'B.Cu'], f'N{i + 1}') for i in range(8)]
    j = footprint('J1', 30, 10, 0, hp)
    return board(40, 20, [u, j])


def test_fanout_stubs_and_staggered_vias():
    b = _load(_soic_board())
    b.netclasses['Default'].track_width = 0.3      # 0.3 + 2 x 0.2 does not pass between 1.27 mm pins
    fo = fanout(b, _nets(b), 0.25)
    assert len(fo) == 8
    vias = [v for segs, vs in fo.values() for v in vs]
    assert len(vias) == 8, 'every SOIC pin gets a via'
    for i, (x1, y1) in enumerate(vias):
        for (x2, y2) in vias[i + 1:]:
            assert ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 >= 0.8 + 0.2 - 1e-6
    assert len(b.vias) == 8 and len(b.tracks) == 8   # added to the board for the routers


def test_fanned_out_soic_routes_complete():
    for engine in (ShapeRouter, Router):
        b = _load(_soic_board())
        b.netclasses['Default'].track_width = 0.3
        nets = _nets(b)
        fanout(b, nets, 0.25)
        r = engine(b, nets, Options(verbose=False))
        r.run()
        assert all(r.routes[n].complete for n in nets), engine.__name__


def test_fanned_out_net_keeps_its_class_width():
    """The neck width is for the stub only: once a fine-pitch pad has its fan-out, the net routes at the class width."""
    b = _load(_soic_board())
    b.netclasses['Default'].track_width = 0.3
    nets = _nets(b)
    fanout(b, nets, 0.25)
    r = ShapeRouter(b, nets, Options(verbose=False, neck_width=0.25))
    r.run()
    for n in nets:
        assert r.routes[n].complete
        widths = {round(s[5], 3) for s in r.routes[n].segments}
        assert widths == {0.3}, widths


def test_fanout_keeps_the_neighbours_clearance_class():
    """A stub next to a pad of a wider-clearance class keeps that class's clearance: KiCad's DRC takes the larger."""
    from tracklayer.board import NetClass
    from tracklayer.fanout import _pad_seg_dist, _pad_point_dist
    b = _load(_soic_board())
    b.netclasses['Default'].track_width = 0.3
    b.netclasses['HV'] = NetClass('HV', clearance=0.8, track_width=1.0, via_diameter=1.2, via_drill=0.6)
    hv = {p.net for p in b.pads if p.ref == 'U1' and p.number in ('2', '6')}
    for n in hv:
        b.net_class_of[n] = 'HV'
    fo = fanout(b, _nets(b) - hv, 0.25)
    hv_pads = [p for p in b.pads if p.net in hv]
    for net, (segs, vias) in fo.items():
        for (layer, x1, y1, x2, y2, w) in segs:
            for q in hv_pads:
                assert _pad_seg_dist(q, x1, y1, x2, y2) >= w / 2 + 0.8 - 1e-6, (net, q.number)
        for (vx, vy) in vias:
            for q in hv_pads:
                assert _pad_point_dist(q, vx, vy) >= 0.8 / 2 + 0.8 - 1e-6, (net, q.number)


def _soic_with_pin1_routed():
    """The SOIC board with pin 1's net already routed by hand: a track off the pad on F.Cu."""
    from tracklayer.board import Track
    b = _load(_soic_board())
    b.netclasses['Default'].track_width = 0.3
    p1 = next(p for p in b.pads if p.ref == 'U1' and p.number == '1')
    b.tracks.append(Track(p1.x, p1.y, p1.x - 3.0, p1.y, 0.25, 'F.Cu', p1.net))
    return b, p1


def test_fanout_leaves_a_pad_that_has_its_track():
    """Routing a board that is mostly done must not fan out the pads that already have copper on them."""
    b, p1 = _soic_with_pin1_routed()
    fo = fanout(b, _nets(b), 0.25)
    assert p1.net not in fo, 'pin 1 is routed already: no stub, no via'
    assert len(fo) == 7


def test_fanout_clears_existing_tracks_of_other_nets():
    """A stub or via of the fan-out must keep the clearance from the tracks already on the board."""
    from tracklayer.fanout import _seg_seg_dist, _seg_dist
    b, p1 = _soic_with_pin1_routed()
    hand = b.tracks[0]
    fo = fanout(b, _nets(b), 0.25)
    for net, (segs, vias) in fo.items():
        for (layer, x1, y1, x2, y2, w) in segs:
            if layer == hand.layer:
                assert _seg_seg_dist((x1, y1, x2, y2), (hand.x1, hand.y1, hand.x2, hand.y2)) >= w / 2 + hand.width / 2 + 0.2 - 1e-6
        for (vx, vy) in vias:
            assert _seg_dist(vx, vy, hand.x1, hand.y1, hand.x2, hand.y2) >= 0.8 / 2 + hand.width / 2 + 0.2 - 1e-6
