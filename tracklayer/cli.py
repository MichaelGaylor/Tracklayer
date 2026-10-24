"""Command line: tracklayer BOARD.kicad_pcb [options]"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .board import load, match_nets
from .router import Options, Router
from .svg import write_svg
from .writer import write_board


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='tracklayer', description='Grid autorouter for KiCad boards.')
    ap.add_argument('board', help='.kicad_pcb to route')
    ap.add_argument('-o', '--output', help='routed board to write (default: <board>-routed.kicad_pcb)')
    ap.add_argument('--in-place', action='store_true', help='write the routes into the input file')
    ap.add_argument('--pro', help='.kicad_pro for net classes (default: next to the board)')
    ap.add_argument('--pitch', type=float, default=0.2, help='grid pitch in mm (default 0.2)')
    ap.add_argument('--layers', help='copper layers to use, comma separated (default: all)')
    ap.add_argument('--nets', nargs='*', default=None, help='only route nets matching these patterns (fnmatch)')
    ap.add_argument('--exclude', nargs='*', default=[], help='never route nets matching these patterns')
    ap.add_argument('--passes', type=int, default=8, help='rip-up and reroute passes (default 8)')
    ap.add_argument('--angles', choices=['45', 'any', '90'], default='45')
    ap.add_argument('--via-cost', type=float, default=4.0, help='mm of track one via is worth (default 4)')
    ap.add_argument('--bend-cost', type=float, default=0.3)
    ap.add_argument('--zone-cost', type=float, default=0.3, help='penalty factor for crossing another net\'s plane-sized pour')
    ap.add_argument('--zone-cost-local', type=float, default=6.0, help='penalty factor for crossing a smaller pour')
    ap.add_argument('--no-zone-nets', action='store_true', help='do not route pads of nets that have pours')
    ap.add_argument('--pour-obstacle', type=float, default=0.0, metavar='FRAC',
                    help='pours covering less than FRAC of the board are hard obstacles for other nets (default off)')
    ap.add_argument('--pour-depth', type=float, default=1.0, help='how far inside its pour a track must end, mm')
    ap.add_argument('--via-in-pad', action='store_true', help='allow vias inside SMD pads of the same net')
    ap.add_argument('--replace', action='store_true', help='remove existing unlocked tracks of the routed nets first')
    ap.add_argument('--neck', type=float, help='width used to escape tight pads (default: the Default class width)')
    ap.add_argument('--width', type=float, help='track width for every routed net (overrides net classes)')
    ap.add_argument('--clearance', type=float, help='clearance for every routed net')
    ap.add_argument('--via', type=float, help='via diameter for every routed net')
    ap.add_argument('--drill', type=float, help='via drill for every routed net')
    ap.add_argument('--margin', type=float, help='grid quantisation margin added to clearances (default 0.71 * pitch)')
    ap.add_argument('--svg', help='also write a picture of the result')
    ap.add_argument('-q', '--quiet', action='store_true')
    ap.add_argument('--version', action='version', version=__version__)
    a = ap.parse_args(argv)

    t0 = time.time()
    b = load(a.board, a.pro)
    all_nets = {code for code in b.nets if code > 0}
    nets = match_nets(b, a.nets) if a.nets else set(all_nets)
    nets -= match_nets(b, a.exclude) if a.exclude else set()
    # nets with fewer than two pads need no routing
    counts: dict[int, int] = {}
    for p in b.pads:
        counts[p.net] = counts.get(p.net, 0) + 1
    nets = {n for n in nets if counts.get(n, 0) >= 2 or any(z.net == n and not z.is_keepout for z in b.zones)}
    doomed = []
    if a.replace:
        # the router must not see the tracks that are about to be removed, and the writer must
        doomed = [t for t in b.tracks if t.net in nets and not t.locked] + [v for v in b.vias if v.net in nets and not v.locked]
        b.tracks = [t for t in b.tracks if not (t.net in nets and not t.locked)]
        b.vias = [v for v in b.vias if not (v.net in nets and not v.locked)]
    opts = Options(pitch=a.pitch, layers=a.layers.split(',') if a.layers else None, passes=a.passes,
                   via_cost=a.via_cost, bend_cost=a.bend_cost, zone_cost=a.zone_cost, zone_cost_local=a.zone_cost_local, angles=a.angles,
                   route_zone_nets=not a.no_zone_nets, via_in_pad=a.via_in_pad, margin=a.margin,
                   pour_obstacle_frac=a.pour_obstacle, pour_depth=a.pour_depth,
                   width=a.width, neck_width=a.neck, clearance=a.clearance, via_diameter=a.via, via_drill=a.drill, verbose=not a.quiet)
    if not nets:
        print('nothing to route', file=sys.stderr)
        return 1
    print(f'{os.path.basename(a.board)}: {len(b.pads)} pads, {len(nets)} nets to route, '
          f'{len(b.tracks)} existing tracks, {len(b.zones)} zones, layers {", ".join(opts.layers or b.copper_layers)}')
    r = Router(b, nets, opts)
    r.run()
    out = a.board if a.in_place else (a.output or os.path.splitext(a.board)[0] + '-routed.kicad_pcb')
    write_board(b, r.routes, doomed, out)
    print(f'wrote {out}')
    if a.svg:
        write_svg(b, r.routes, a.svg, r.unrouted)
        print(f'wrote {a.svg}')
    print(f'total {time.time() - t0:.1f} s')
    return 0 if not r.unrouted else 2


if __name__ == '__main__':
    sys.exit(main())
