import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from tracklayer.board import load
from tracklayer.router import Options, Router
from tracklayer.shape_router import ShapeRouter

from boards import board, footprint

ZONE = ('\t(zone\n\t\t(net 0)\n\t\t(net_name "")\n\t\t(layers "F&B.Cu")\n\t\t(name "band keepout classes: Default")\n'
        '\t\t(keepout\n\t\t\t(tracks allowed)\n\t\t\t(vias allowed)\n\t\t\t(pads allowed)\n\t\t\t(copperpour allowed)\n\t\t\t(footprints allowed)\n\t\t)\n'
        '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy 16 0)\n\t\t\t\t(xy 20 0)\n\t\t\t\t(xy 20 20)\n\t\t\t\t(xy 16 20)\n\t\t\t)\n\t\t)\n\t)\n')


def _board_with_classes():
    """A signal (class Default) and a supply (class Power) both have to cross a band that only Default must avoid."""
    smd = ('smd', 'rect', ['F.Cu'])
    l = footprint('L1', 6, 10, 0, [('1', *smd[:2], 0, -5, 1.0, 1.2, smd[2], 'SIG'), ('2', *smd[:2], 0, 5, 1.0, 1.2, smd[2], 'PWR')])
    r = footprint('R1', 30, 10, 0, [('1', *smd[:2], 0, -5, 1.0, 1.2, smd[2], 'SIG'), ('2', *smd[:2], 0, 5, 1.0, 1.2, smd[2], 'PWR')])
    text = board(36, 20, [l, r], ZONE)
    d = tempfile.mkdtemp()
    p = os.path.join(d, 't.kicad_pcb')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)
    pro = {'net_settings': {'classes': [{'name': 'Default', 'clearance': 0.2, 'track_width': 0.25, 'via_diameter': 0.8, 'via_drill': 0.4},
                                        {'name': 'Power', 'clearance': 0.2, 'track_width': 0.25, 'via_diameter': 0.8, 'via_drill': 0.4}],
                            'netclass_patterns': [{'netclass': 'Power', 'pattern': 'PWR'}]}}
    with open(os.path.join(d, 't.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump(pro, f)
    return load(p)


def test_class_selective_rule_area():
    b = _board_with_classes()
    z = [z for z in b.zones if z.is_keepout][0]
    assert z.keepout_classes == ['Default'] and z.keepout_tracks
    sig, pwr = b.net_code('SIG'), b.net_code('PWR')
    for engine in (Router, ShapeRouter):
        r = engine(b, {sig, pwr}, Options(verbose=False))
        r.run()
        assert pwr in r.routes and r.routes[pwr].complete, engine.__name__      # Power may cross the band
        assert sig not in r.routes or not r.routes[sig].complete, engine.__name__   # Default may not: the band spans the board
