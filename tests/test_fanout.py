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
