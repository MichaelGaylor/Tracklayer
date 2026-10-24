"""Write routed tracks back into the .kicad_pcb text.

New segments and vias are appended before the file's closing parenthesis, which every KiCad
version reads back without complaint. Nothing else in the file is touched, so hand-routed work,
zones and footprints survive byte for byte. With `replace_nets`, existing unlocked tracks and vias
of those nets are removed first.
"""
from __future__ import annotations

import uuid

from .board import Board
from .sexp import fmt


def write_board(board: Board, routes, remove=None, out_path: str | None = None) -> str:
    """`remove`: existing Track/Via objects (with their source spans) to delete from the file first."""
    text = board.text
    if remove:
        spans = [it.span for it in remove if it.span]
        for a, b in sorted(set(spans), reverse=True):
            # also eat the whitespace before the node so the file stays tidy
            while a > 0 and text[a - 1] in ' \t':
                a -= 1
            if a > 0 and text[a - 1] == '\n':
                a -= 1
            text = text[:a] + text[b:]
    end = text.rstrip().rfind(')')
    head, tail = text[:end], text[end:]
    lines = []
    for r in routes.values():
        w, _, vd, vdr = r.params
        net_s = f'(net "{board.net_name(r.net)}")' if board.nets_by_name else f'(net {r.net})'
        for layer, x1, y1, x2, y2, ws in r.segments:
            lines.append(f'\t(segment\n\t\t(start {fmt(x1)} {fmt(y1)})\n\t\t(end {fmt(x2)} {fmt(y2)})\n'
                         f'\t\t(width {fmt(ws)})\n\t\t(layer "{layer}")\n\t\t{net_s}\n\t\t(uuid "{uuid.uuid4()}")\n\t)')
        for x, y in r.vias:
            lays = ' '.join(f'"{l}"' for l in (r.via_layers or ('F.Cu', 'B.Cu')))
            lines.append(f'\t(via\n\t\t(at {fmt(x)} {fmt(y)})\n\t\t(size {fmt(vd)})\n\t\t(drill {fmt(vdr)})\n'
                         f'\t\t(layers {lays})\n\t\t{net_s}\n\t\t(uuid "{uuid.uuid4()}")\n\t)')
    if lines:
        if not head.endswith('\n'):
            head += '\n'
        head += '\n'.join(lines) + '\n'
    out = head + tail
    if out_path:
        with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(out)
    return out
