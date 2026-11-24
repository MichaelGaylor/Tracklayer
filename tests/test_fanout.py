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


def _one_stub():
    """The SOIC board fanned out, and pin 1's stub and via picked out, with where the net's header pin is:
    (board, fan-out, net, stub, via, header). The tests draw the rest of the net as one straight track to the header."""
    b = _load(_soic_board())
    b.netclasses['Default'].track_width = 0.3
    fo = fanout(b, _nets(b), 0.25)
    p1 = next(p for p in b.pads if p.ref == 'U1' and p.number == '1')
    segs, vias = fo[p1.net]
    assert len(segs) == 1 and len(vias) == 1
    h = next(p for p in b.pads if p.ref == 'J1' and p.net == p1.net)
    return b, fo, p1.net, segs[0], vias[0], (h.x, h.y)


def _routed(net, segments, complete=True):
    from tracklayer.router import NetRoute
    return {net: NetRoute(net, segments=list(segments), complete=complete)}


def test_prune_keeps_a_via_the_route_leaves_on_another_layer():
    from tracklayer.fanout import prune
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    assert prune(_routed(net, [('B.Cu', vx, vy, hx, hy, 0.3)]), fo, b) == (0, 0)
    assert fo[net] == ([stub], [(vx, vy)])


def test_prune_drops_a_via_the_route_passes_on_the_same_layer():
    """The track carries on from the stub's end on the stub's own layer: the stub stays, the hole goes."""
    from tracklayer.fanout import prune
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    n_vias = len(b.vias)
    assert prune(_routed(net, [('F.Cu', vx, vy, hx, hy, 0.3)]), fo, b) == (1, 0)
    assert fo[net] == ([stub], [])
    assert len(b.vias) == n_vias - 1


def test_prune_drops_a_stub_and_via_nothing_uses():
    """The net left its pad some other way: stub and via are a spur with a hole on the end."""
    from tracklayer.fanout import prune
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    px, py = stub[1], stub[2]
    assert prune(_routed(net, [('F.Cu', px, py, hx, hy, 0.3)]), fo, b) == (1, 1)
    assert fo[net] == ([], [])


def test_prune_swaps_a_junction_via_for_a_short_segment():
    """A track that ends on the via's rim touches the via but not the stub: the via goes only because a
    short segment takes over the join, so the track is not left hanging."""
    from tracklayer.fanout import prune, _touch
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    track = ('F.Cu', vx - 0.35, vy, vx - 0.35, vy - 3, 0.3)       # starts beyond the stub's end, on the rim
    assert prune(_routed(net, [track, ('F.Cu', vx - 0.35, vy - 3, hx, hy, 0.3)]), fo, b) == (1, 0)
    segs, vias = fo[net]
    assert vias == [] and len(segs) == 2 and segs[0] == stub
    assert _touch(('s', *segs[1]), ('s', *stub), True) and _touch(('s', *segs[1]), ('s', *track), True)


def test_prune_keeps_a_junction_via_with_no_room_for_the_segment():
    """The same, but another net's track runs where the joining segment would go: the via stays."""
    from tracklayer.board import Track
    from tracklayer.fanout import prune
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    other = next(n for n in fo if n != net)
    b.tracks.append(Track(vx - 0.2, vy - 0.3, vx - 0.2, vy + 0.3, 0.1, 'F.Cu', other))
    route = [('F.Cu', vx - 0.35, vy, vx - 0.35, vy - 3, 0.3), ('F.Cu', vx - 0.35, vy - 3, hx, hy, 0.3)]
    assert prune(_routed(net, route), fo, b) == (0, 0)
    assert fo[net] == ([stub], [(vx, vy)])


def test_prune_trims_the_spur_the_router_ran_to_an_unused_via():
    """The net leaves the pad directly, and a second track runs from the pad to the via's rim: with the
    via gone that track ends on nothing, so it goes too."""
    from tracklayer.fanout import prune
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    px, py = stub[1], stub[2]
    main = ('F.Cu', px, py, hx, hy, 0.3)
    spur = ('F.Cu', px, py + 0.2, vx + 0.3, vy + 0.2, 0.3)
    routes = _routed(net, [main, spur])
    assert prune(routes, fo, b) == (1, 2)
    assert fo[net] == ([], []) and routes[net].segments == [main]


def test_prune_leaves_an_incomplete_net_alone():
    from tracklayer.fanout import prune
    b, fo, net, stub, (vx, vy), (hx, hy) = _one_stub()
    assert prune(_routed(net, [], complete=False), fo, b) == (0, 0)
    assert prune({}, fo, b) == (0, 0)
    assert fo[net] == ([stub], [(vx, vy)])


def test_no_unused_fanout_via_survives_routing():
    """After a full route, every fan-out via left is reached by a track on a layer other than its stub's."""
    from tracklayer.fanout import prune, merge, _seg_dist
    from tracklayer.router import NetRoute
    b = _load(_soic_board())
    b.netclasses['Default'].track_width = 0.3
    nets = _nets(b)
    fo = fanout(b, nets, 0.25)
    r = ShapeRouter(b, nets, Options(verbose=False, neck_width=0.25))
    r.run()
    prune(r.routes, fo, b)
    stubs = {n: list(s) for n, (s, v) in fo.items()}
    merge(r.routes, fo, b, NetRoute)
    for n, (segs, vias) in fo.items():
        for (vx, vy) in vias:
            layer = next(s[0] for s in stubs[n] if (s[3], s[4]) == (vx, vy))
            assert any(s[0] != layer and _seg_dist(vx, vy, *s[1:5]) < 0.4 + s[5] / 2 for s in r.routes[n].segments), n
