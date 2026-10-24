"""Quick SVG picture of the result: outline, pads, pours, existing copper and the new routes."""
from __future__ import annotations

import math

from .board import Board
from .sexp import fmt

LAYER_COLOURS = {'F.Cu': '#d8323c', 'B.Cu': '#3864d8', 'In1.Cu': '#c8a000', 'In2.Cu': '#20a060',
                 'In3.Cu': '#b040c0', 'In4.Cu': '#20b0c0', 'In5.Cu': '#e07020', 'In6.Cu': '#80a040'}


def _colour(layer: str) -> str:
    return LAYER_COLOURS.get(layer, '#888')


def write_svg(board: Board, routes, path: str, unrouted: list[int] | None = None, scale: float = 8.0) -> None:
    x0, y0, x1, y1 = board.bbox()
    m = 2.0
    x0 -= m; y0 -= m; x1 += m; y1 += m
    W, H = (x1 - x0) * scale, (y1 - y0) * scale
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{fmt(x0)} {fmt(y0)} {fmt(x1 - x0)} {fmt(y1 - y0)}" '
           f'width="{W:.0f}" height="{H:.0f}">',
           f'<rect x="{fmt(x0)}" y="{fmt(y0)}" width="{fmt(x1 - x0)}" height="{fmt(y1 - y0)}" fill="#1d1f24"/>']
    for poly in board.outline:
        out.append('<polygon points="' + ' '.join(f'{fmt(x)},{fmt(y)}' for x, y in poly) + '" fill="#262a30" stroke="#e8c44a" stroke-width="0.15"/>')
    for z in board.zones:
        if z.is_keepout:
            fill, op = '#ff00ff', '0.25'
        else:
            fill, op = _colour(z.layers[0] if z.layers else 'F.Cu'), '0.12'
        out.append('<polygon points="' + ' '.join(f'{fmt(x)},{fmt(y)}' for x, y in z.polygon) + f'" fill="{fill}" fill-opacity="{op}" stroke="{fill}" stroke-width="0.05"/>')
    for t in board.tracks:
        out.append(f'<line x1="{fmt(t.x1)}" y1="{fmt(t.y1)}" x2="{fmt(t.x2)}" y2="{fmt(t.y2)}" stroke="{_colour(t.layer)}" stroke-opacity="0.6" stroke-width="{fmt(t.width)}" stroke-linecap="round"/>')
    for v in board.vias:
        out.append(f'<circle cx="{fmt(v.x)}" cy="{fmt(v.y)}" r="{fmt(v.size / 2)}" fill="#999" fill-opacity="0.6"/>')
    unrouted = set(unrouted or [])
    for p in board.pads:
        col = '#c8c8c8' if p.through else _colour(p.layers[0])
        if p.net in unrouted:
            col = '#ff30ff'
        cr = p.corner_radius()
        out.append(f'<rect x="{fmt(-p.w / 2)}" y="{fmt(-p.h / 2)}" width="{fmt(p.w)}" height="{fmt(p.h)}" rx="{fmt(cr)}" '
                   f'fill="{col}" fill-opacity="0.85" transform="translate({fmt(p.x)} {fmt(p.y)}) rotate({fmt(-p.rot)})"/>')
        if p.through and p.drill > 0:
            out.append(f'<circle cx="{fmt(p.x)}" cy="{fmt(p.y)}" r="{fmt(p.drill / 2)}" fill="#1d1f24"/>')
    for r in routes.values():
        for layer, ax, ay, bx, by, w in r.segments:
            out.append(f'<line x1="{fmt(ax)}" y1="{fmt(ay)}" x2="{fmt(bx)}" y2="{fmt(by)}" stroke="{_colour(layer)}" stroke-width="{fmt(w)}" stroke-linecap="round"/>')
        for x, y in r.vias:
            out.append(f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="{fmt(r.params[2] / 2)}" fill="#e0e0e0"/>'
                       f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="{fmt(r.params[3] / 2)}" fill="#1d1f24"/>')
    # ratsnest for what is left
    for n in unrouted:
        ps = [p for p in board.pads if p.net == n]
        for a, b in zip(ps, ps[1:]):
            out.append(f'<line x1="{fmt(a.x)}" y1="{fmt(a.y)}" x2="{fmt(b.x)}" y2="{fmt(b.y)}" stroke="#ff30ff" stroke-width="0.1" stroke-dasharray="0.5 0.3"/>')
    out.append('</svg>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
