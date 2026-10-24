import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from tracklayer.router import Options, Router, _seg_dist
from tracklayer.shape_router import ShapeRouter

from boards import header_and_chip
from test_router import _load, _nets

VIA = ('\t(via\n\t\t(at 15 10)\n\t\t(size 1.2)\n\t\t(drill 0.6)\n'
       '\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "D0")\n\t)\n')


def test_existing_via_blocks_inner_layers():
    """A through via already on a six-layer board is an obstacle on the inner layers too."""
    text = header_and_chip(layers=6)
    text = text[:text.rstrip().rfind(')')] + VIA + ')\n'
    b = _load(text)
    assert len(b.vias) == 1 and b.vias[0].layers == b.copper_layers
    nets = _nets(b) - {b.net_code('D0')}
    for engine in (ShapeRouter, Router):
        r = engine(b, nets, Options(verbose=False))
        r.run()
        for rt in r.routes.values():
            for layer, x1, y1, x2, y2, w in rt.segments:
                d = _seg_dist(x1, y1, x2, y2, 15, 10, 15, 10)
                assert d >= 0.6 + w / 2 + 0.2 - 1e-6, (engine.__name__, layer, d)
