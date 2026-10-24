"""Tiny boards written in KiCad's text format, for the tests."""


def footprint(ref: str, x: float, y: float, rot: float, pads: list[tuple]) -> str:
    """pads: (number, kind, shape, lx, ly, w, h, layers, net_name)"""
    out = [f'\t(footprint "test:{ref}"\n\t\t(layer "F.Cu")\n\t\t(at {x} {y} {rot})\n'
           f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))']
    for num, kind, shape, lx, ly, w, h, layers, net in pads:
        drill = '\t\t\t(drill 0.8)\n' if kind == 'thru_hole' else ''
        lay = ' '.join(f'"{l}"' for l in layers)
        out.append(f'\t\t(pad "{num}" {kind} {shape}\n\t\t\t(at {lx} {ly} {rot})\n\t\t\t(size {w} {h})\n{drill}'
                   f'\t\t\t(layers {lay})\n\t\t\t(net "{net}")\n\t\t)')
    out.append('\t)')
    return '\n'.join(out)


def board(width: float, height: float, footprints: list[str], extra: str = '', layers: int = 2) -> str:
    inner = ''.join(f'\t\t({2 * k + 2} "In{k}.Cu" signal)\n' for k in range(1, layers - 1))
    return (f'(kicad_pcb\n\t(version 20260206)\n\t(generator "test")\n'
            f'\t(layers\n\t\t(0 "F.Cu" signal)\n{inner}\t\t(2 "B.Cu" signal)\n\t\t(44 "Edge.Cuts" user)\n\t)\n'
            f'\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n'
            f'\t(gr_rect\n\t\t(start 0 0)\n\t\t(end {width} {height})\n\t\t(layer "Edge.Cuts")\n\t\t(stroke (width 0.1) (type default))\n\t)\n'
            + '\n'.join(footprints) + '\n' + extra + ')\n')


def two_resistors() -> str:
    """R1 and R2 side by side, nets A and B crossing so one of them needs a via or a detour."""
    smd = ('smd', 'roundrect', ['F.Cu'])
    r1 = footprint('R1', 10, 10, 0, [('1', *smd[:2], -1, 0, 1.0, 1.4, smd[2], 'A'), ('2', *smd[:2], 1, 0, 1.0, 1.4, smd[2], 'B')])
    r2 = footprint('R2', 20, 10, 0, [('1', *smd[:2], -1, 0, 1.0, 1.4, smd[2], 'B'), ('2', *smd[:2], 1, 0, 1.0, 1.4, smd[2], 'A')])
    return board(30, 20, [r1, r2])


def header_and_chip(layers: int = 2) -> str:
    """A 2x4 through-hole header wired to an 8-pin SOIC: eight nets, some must cross."""
    pins = []
    nets = ['D0', 'D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7']
    k = 0
    for col in range(4):
        for row in range(2):
            pins.append((str(k + 1), 'thru_hole', 'circle', col * 2.54, row * 2.54, 1.7, 1.7, ['*.Cu'], nets[k]))
            k += 1
    j1 = footprint('J1', 6, 6, 0, pins)
    soic = []
    order = ['D7', 'D5', 'D3', 'D1', 'D0', 'D2', 'D4', 'D6']   # scrambled so tracks must cross
    for n in range(8):
        side = -1 if n < 4 else 1
        idx = n if n < 4 else 7 - n
        soic.append((str(n + 1), 'smd', 'rect', side * 2.7, -1.905 + idx * 1.27, 1.5, 0.6, ['F.Cu'], order[n]))
    u1 = footprint('U1', 24, 10, 0, soic)
    return board(36, 20, [j1, u1], layers=layers)


def with_pour() -> str:
    """Two GND pads outside a GND pour on B.Cu, plus a signal that must cross the pour."""
    smd = ('smd', 'rect', ['F.Cu'])
    c1 = footprint('C1', 8, 5, 90, [('1', *smd[:2], -1, 0, 1.0, 1.2, smd[2], 'GND'), ('2', *smd[:2], 1, 0, 1.0, 1.2, smd[2], 'SIG')])
    c2 = footprint('C2', 28, 15, 0, [('1', *smd[:2], -1, 0, 1.0, 1.2, smd[2], 'SIG'), ('2', *smd[:2], 1, 0, 1.0, 1.2, smd[2], 'GND')])
    zone = ('\t(zone\n\t\t(net "GND")\n\t\t(layer "B.Cu")\n\t\t(priority 0)\n'
            '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy 2 2)\n\t\t\t\t(xy 34 2)\n\t\t\t\t(xy 34 18)\n\t\t\t\t(xy 2 18)\n\t\t\t)\n\t\t)\n\t)\n')
    return board(36, 20, [c1, c2], zone)
