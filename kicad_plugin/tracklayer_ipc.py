"""Tracklayer for KiCad 9 and later: an IPC API plugin.

KiCad starts this script as its own process (see plugin.json) with the API socket in the
environment. The open board, unsaved edits included, is fetched as text through the API, routed
by Tracklayer in a worker thread while a small window shows the log, and the tracks and vias go
back through the API as one commit, so Undo takes them out again. Zones are refilled afterwards.
Nothing on disk is touched.
"""
import os
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from kipy import KiCad                                   # noqa: E402
from kipy.board_types import BoardLayer, Track, Via      # noqa: E402
from kipy.geometry import Vector2                        # noqa: E402
from kipy.util.units import from_mm, to_mm               # noqa: E402

from tracklayer import __version__                       # noqa: E402
from tracklayer.board import NetClass, load, match_nets  # noqa: E402
from tracklayer.router import Options, Router            # noqa: E402
from tracklayer.shape_router import ShapeRouter          # noqa: E402

SITE = 'www.tiggyengineering.com'


def fetch_board(kicad):
    """The open board as a Tracklayer Board, with the net classes KiCad resolved."""
    board = kicad.get_board()
    text = board.get_as_string()
    d = tempfile.mkdtemp(prefix='tracklayer-')
    path = os.path.join(d, 'board.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    pro = None
    try:
        proj = board.get_project()
        if proj.path and proj.name:
            cand = os.path.join(proj.path, proj.name + '.kicad_pro')
            pro = cand if os.path.exists(cand) else None
    except Exception:
        pass
    b = load(path, pro)
    # net classes straight from KiCad, so they are right even without a project file
    try:
        nets = board.get_nets()
        by_name = board.get_netclass_for_nets(nets)
        for name, nc in by_name.items():
            code = b.net_code(name)
            if code is None:
                continue
            b.netclasses[nc.name] = NetClass(nc.name, to_mm(nc.clearance), to_mm(nc.track_width),
                                             to_mm(nc.via_diameter), to_mm(nc.via_drill))
            b.net_class_of[code] = nc.name
    except Exception as e:
        print('net classes from KiCad unavailable:', e)
    return board, b


def layer_enum(name: str):
    """'F.Cu' -> BoardLayer.BL_F_Cu, 'In1.Cu' -> BL_In1_Cu (works on every API version)."""
    return BoardLayer.Value('BL_' + name.replace('.', '_'))


def apply_routes(board, b, routes, log):
    nets = {n.name: n for n in board.get_nets()}
    items = []
    for r in routes.values():
        net = nets.get(b.net_name(r.net))
        if net is None:
            continue
        for layer, x1, y1, x2, y2, w in r.segments:
            t = Track()
            t.start = Vector2.from_xy_mm(x1, y1)
            t.end = Vector2.from_xy_mm(x2, y2)
            t.width = from_mm(w)
            t.layer = layer_enum(layer)
            t.net = net
            items.append(t)
        _, _, vd, vdr = r.params
        for x, y in r.vias:
            v = Via()
            v.position = Vector2.from_xy_mm(x, y)
            v.diameter = from_mm(vd)
            v.drill_diameter = from_mm(vdr)
            v.net = net
            items.append(v)
    if not items:
        return 0
    commit = board.begin_commit()
    board.create_items(items)
    board.push_commit(commit, 'Tracklayer autoroute')
    return len(items)


class App:
    def __init__(self, root):
        self.root = root
        root.title('Tracklayer autoroute')
        root.minsize(620, 520)
        f = ttk.Frame(root, padding=10)
        f.pack(fill='both', expand=True)
        self.vars = {}

        def field(row, col, label, value, tip=''):
            ttk.Label(f, text=label).grid(row=row, column=col * 2, sticky='w', padx=(0, 6), pady=3)
            v = tk.StringVar(value=value)
            e = ttk.Entry(f, textvariable=v, width=14)
            e.grid(row=row, column=col * 2 + 1, sticky='ew', pady=3)
            self.vars[label] = v
            return v

        self.engine = tk.StringVar(value='shape')
        ttk.Label(f, text='Engine').grid(row=0, column=0, sticky='w')
        eng = ttk.Combobox(f, textvariable=self.engine, values=['shape', 'grid'], state='readonly', width=12)
        eng.grid(row=0, column=1, sticky='ew', pady=3)
        self.angles = tk.StringVar(value='45')
        ttk.Label(f, text='Corners (grid)').grid(row=0, column=2, sticky='w', padx=(12, 6))
        ttk.Combobox(f, textvariable=self.angles, values=['45', 'any', '90'], state='readonly', width=12).grid(row=0, column=3, sticky='ew')
        self.pitch = field(1, 0, 'Grid / via pitch (mm)', '0.2')
        self.passes = field(1, 1, 'Rip-up passes', '4')
        self.via_cost = field(2, 0, 'Via cost (mm)', '4')
        self.bend_cost = field(2, 1, 'Bend cost (mm)', '0.3')
        self.nets = field(3, 0, 'Only nets (patterns)', '')
        self.exclude = field(3, 1, 'Skip nets (patterns)', '')
        self.layer_costs = field(4, 0, 'Layer costs', '')
        ttk.Label(f, text='e.g. F.Cu=1.6: long runs avoid dear layers').grid(row=4, column=2, columnspan=2, sticky='w', padx=(12, 0))
        self.replace = tk.BooleanVar(value=False)
        self.refill = tk.BooleanVar(value=True)
        self.zone_nets = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='Remove existing unlocked tracks of the routed nets first', variable=self.replace).grid(row=5, column=0, columnspan=4, sticky='w')
        ttk.Checkbutton(f, text='Refill zones afterwards', variable=self.refill).grid(row=6, column=0, columnspan=4, sticky='w')
        ttk.Checkbutton(f, text='Route pads of pour nets that lie outside their pour', variable=self.zone_nets).grid(row=7, column=0, columnspan=4, sticky='w')
        self.log = tk.Text(f, height=16, width=80, state='disabled', font=('Consolas', 9))
        self.log.grid(row=8, column=0, columnspan=4, sticky='nsew', pady=(8, 8))
        sb = ttk.Scrollbar(f, command=self.log.yview)
        sb.grid(row=8, column=4, sticky='ns')
        self.log['yscrollcommand'] = sb.set
        bar = ttk.Frame(f)
        bar.grid(row=9, column=0, columnspan=4, sticky='e')
        self.run_btn = ttk.Button(bar, text='Route', command=self.on_run)
        self.run_btn.pack(side='left', padx=(0, 8))
        self.close_btn = ttk.Button(bar, text='Close', command=root.destroy)
        self.close_btn.pack(side='left')
        f.columnconfigure(1, weight=1); f.columnconfigure(3, weight=1); f.rowconfigure(8, weight=1)
        self.say(f'Tracklayer {__version__} by Tiggy Engineering  -  {SITE}')
        try:
            self.kicad = KiCad()
            self.say(f'Connected to KiCad {self.kicad.get_version().full_version}')
        except Exception as e:
            self.kicad = None
            self.say(f'Could not connect to KiCad: {e}')
            self.say('Is the API enabled? Preferences > Plugins > Enable KiCad API.')
            self.run_btn['state'] = 'disabled'

    def say(self, msg):
        def _do():
            self.log['state'] = 'normal'
            self.log.insert('end', msg + '\n')
            self.log.see('end')
            self.log['state'] = 'disabled'
        self.root.after(0, _do)

    def on_run(self):
        try:
            lcs = {}
            for item in self.layer_costs.get().split():
                name, _, val = item.partition('=')
                lcs[name.strip()] = float(val or 1.0)
            opts = Options(engine=self.engine.get(), pitch=float(self.pitch.get()), via_pitch=float(self.pitch.get()), layer_costs=lcs,
                           passes=int(self.passes.get()), via_cost=float(self.via_cost.get()),
                           bend_cost=float(self.bend_cost.get()), angles=self.angles.get(),
                           route_zone_nets=self.zone_nets.get(), verbose=False)
        except ValueError as e:
            self.say(f'Check the numbers: {e}')
            return
        self.run_btn['state'] = 'disabled'; self.close_btn['state'] = 'disabled'
        threading.Thread(target=self._work, args=(opts,), daemon=True).start()

    def _work(self, opts):
        try:
            self.say('Fetching the board from KiCad...')
            board, b = fetch_board(self.kicad)
            all_nets = {c for c in b.nets if c > 0}
            pats = self.nets.get().split()
            nets = match_nets(b, pats) if pats else set(all_nets)
            excl = self.exclude.get().split()
            if excl:
                nets -= match_nets(b, excl)
            counts = {}
            for p in b.pads:
                counts[p.net] = counts.get(p.net, 0) + 1
            nets = {n for n in nets if counts.get(n, 0) >= 2 or any(z.net == n and not z.is_keepout for z in b.zones)}
            removed = 0
            if self.replace.get():
                names = {b.net_name(n) for n in nets}
                old = [t for t in board.get_tracks() if t.net and t.net.name in names and not t.locked]
                old += [v for v in board.get_vias() if v.net and v.net.name in names and not v.locked]
                if old:
                    board.remove_items(old)
                    removed = len(old)
                b.tracks = [t for t in b.tracks if not (t.net in nets and not t.locked)]
                b.vias = [v for v in b.vias if not (v.net in nets and not v.locked)]
            self.say(f'{len(b.pads)} pads, {len(nets)} nets to route, {len(b.tracks)} existing tracks kept'
                     + (f', {removed} removed' if removed else ''))
            r = ShapeRouter(b, nets, opts) if opts.engine == 'shape' else Router(b, nets, opts)
            orig = r.say

            def say(*a):
                orig(*a)
                self.say(' '.join(str(x) for x in a))
            r.say = say
            r.run()
            n = apply_routes(board, b, r.routes, self.say)
            self.say(f'Added {n} items to the board as one commit (Undo removes them)')
            if self.refill.get():
                self.say('Refilling zones...')
                board.refill_zones()
            if r.unrouted:
                self.say('Left for hand routing: ' + ', '.join(b.net_name(x) for x in r.unrouted))
            self.say('Done. Run DRC (Inspect > Design Rules Checker) to confirm.')
        except Exception as e:
            import traceback
            self.say('Failed: ' + ''.join(traceback.format_exception(type(e), e, e.__traceback__)))
        finally:
            self.root.after(0, lambda: (self.run_btn.config(state='normal'), self.close_btn.config(state='normal')))


def main():
    root = tk.Tk()
    try:
        root.iconphoto(True, tk.PhotoImage(file=os.path.join(HERE, 'icon.png')))
    except Exception:
        pass
    app = App(root)
    if os.environ.get('TRACKLAYER_AUTORUN'):      # for the test harness: route, then close when done
        def poll():
            if 'Done.' in app.log.get('1.0', 'end') or 'Failed' in app.log.get('1.0', 'end'):
                print(app.log.get('1.0', 'end'))
                root.destroy()
            else:
                root.after(500, poll)
        root.after(1000, app.on_run)
        root.after(1500, poll)
    root.mainloop()


if __name__ == '__main__':
    main()
