"""Tracklayer for KiCad 9 and later: an IPC API plugin.

KiCad starts this script as its own process (see plugin.json) with the API socket in the
environment. The open board, unsaved edits included, is fetched as text through the API, routed
by Tracklayer in a worker thread while a window shows the log, and the tracks and vias go back
through the API as one commit, so Undo takes them out again. Zones are refilled afterwards.
Nothing on disk is touched.

The window is wxPython when it is available (KiCad's own Python ships it on Windows and macOS),
tkinter otherwise, and with neither it routes with the defaults and reports in a message box.
"""
import os
import sys
import tempfile
import threading

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


# ----------------------------------------------------------------------------- KiCad side

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


def apply_routes(board, b, routes):
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


class Settings:
    """What the dialogs collect."""
    engine = 'shape'
    angles = '45'
    pitch = 0.2
    passes = 4
    via_cost = 4.0
    bend_cost = 0.3
    nets = ''
    exclude = ''
    layer_costs = ''
    replace = False
    refill = True
    zone_nets = True

    def options(self) -> Options:
        lcs = {}
        for item in self.layer_costs.split():
            name, _, val = item.partition('=')
            lcs[name.strip()] = float(val or 1.0)
        return Options(engine=self.engine, pitch=self.pitch, via_pitch=self.pitch, passes=self.passes,
                       via_cost=self.via_cost, bend_cost=self.bend_cost, angles=self.angles, layer_costs=lcs,
                       route_zone_nets=self.zone_nets, verbose=False)


def run_routing(kicad, st: Settings, say):
    """The whole job: fetch, route, push, refill. `say` gets log lines; returns the router."""
    say('Fetching the board from KiCad...')
    board, b = fetch_board(kicad)
    all_nets = {c for c in b.nets if c > 0}
    pats = st.nets.split()
    nets = match_nets(b, pats) if pats else set(all_nets)
    excl = st.exclude.split()
    if excl:
        nets -= match_nets(b, excl)
    counts = {}
    for p in b.pads:
        counts[p.net] = counts.get(p.net, 0) + 1
    nets = {n for n in nets if counts.get(n, 0) >= 2 or any(z.net == n and not z.is_keepout for z in b.zones)}
    removed = 0
    if st.replace:
        names = {b.net_name(n) for n in nets}
        old = [t for t in board.get_tracks() if t.net and t.net.name in names and not t.locked]
        old += [v for v in board.get_vias() if v.net and v.net.name in names and not v.locked]
        if old:
            board.remove_items(old)
            removed = len(old)
        b.tracks = [t for t in b.tracks if not (t.net in nets and not t.locked)]
        b.vias = [v for v in b.vias if not (v.net in nets and not v.locked)]
    say(f'{len(b.pads)} pads, {len(nets)} nets to route, {len(b.tracks)} existing tracks kept'
        + (f', {removed} removed' if removed else ''))
    opts = st.options()
    r = ShapeRouter(b, nets, opts) if opts.engine == 'shape' else Router(b, nets, opts)
    orig = r.say

    def _say(*a):
        orig(*a)
        say(' '.join(str(x) for x in a))
    r.say = _say
    r.run()
    n = apply_routes(board, b, r.routes)
    say(f'Added {n} items to the board as one commit (Undo removes them)')
    if st.refill:
        say('Refilling zones...')
        board.refill_zones()
    if r.unrouted:
        say('Left for hand routing: ' + ', '.join(b.net_name(x) for x in r.unrouted))
    say('Done. Run DRC (Inspect > Design Rules Checker) to confirm.')
    return r


def connect(say):
    try:
        kicad = KiCad()
        say(f'Tracklayer {__version__} by Tiggy Engineering  -  {SITE}')
        say(f'Connected to KiCad {kicad.get_version().full_version}')
        return kicad
    except Exception as e:
        say(f'Could not connect to KiCad: {e}')
        say('Is the API enabled? Preferences > Plugins > Enable KiCad API.')
        return None


# ----------------------------------------------------------------------------- wx window

def main_wx():
    import wx

    class Dialog(wx.Dialog):
        def __init__(self):
            super().__init__(None, title='Tracklayer autoroute', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            p = wx.Panel(self)
            grid = wx.FlexGridSizer(cols=4, vgap=6, hgap=8)
            grid.AddGrowableCol(1); grid.AddGrowableCol(3)

            def field(label, value, tip=''):
                grid.Add(wx.StaticText(p, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                c = wx.TextCtrl(p, value=str(value))
                if tip:
                    c.SetToolTip(tip)
                grid.Add(c, 1, wx.EXPAND)
                return c

            grid.Add(wx.StaticText(p, label='Engine'), 0, wx.ALIGN_CENTER_VERTICAL)
            self.engine = wx.Choice(p, choices=['shape (exact geometry, any angle)', 'grid (45 degree tracks)'])
            self.engine.SetSelection(0)
            grid.Add(self.engine, 1, wx.EXPAND)
            grid.Add(wx.StaticText(p, label='Corners (grid engine)'), 0, wx.ALIGN_CENTER_VERTICAL)
            self.angles = wx.Choice(p, choices=['45 degrees', 'any angle', '90 degrees'])
            self.angles.SetSelection(0)
            grid.Add(self.angles, 1, wx.EXPAND)
            self.pitch = field('Grid / via pitch (mm)', '0.2', 'Grid engine: grid pitch. Shape engine: via lattice pitch.')
            self.passes = field('Rip-up passes', '4')
            self.via_cost = field('Via cost (mm of track)', '4', 'Raise for fewer vias')
            self.bend_cost = field('Bend cost (mm)', '0.3')
            self.nets = field('Only nets (patterns)', '', 'Space separated, e.g. D* CLK; empty routes everything unrouted')
            self.exclude = field('Skip nets (patterns)', '')
            self.layer_costs = field('Layer costs', '', 'e.g. F.Cu=1.6: long runs avoid dear layers; space separated')
            grid.Add(wx.StaticText(p, label=''), 0); grid.Add(wx.StaticText(p, label=''), 0)
            self.replace = wx.CheckBox(p, label='Remove existing unlocked tracks of the routed nets first')
            self.refill = wx.CheckBox(p, label='Refill zones afterwards'); self.refill.SetValue(True)
            self.zone_nets = wx.CheckBox(p, label='Route pads of pour nets that lie outside their pour'); self.zone_nets.SetValue(True)
            self.log = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(600, 240))
            self.log.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            self.run_btn = wx.Button(p, label='Route')
            self.close_btn = wx.Button(p, wx.ID_CANCEL, label='Close')
            self.run_btn.Bind(wx.EVT_BUTTON, self.on_run)
            v = wx.BoxSizer(wx.VERTICAL)
            v.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
            for c in (self.replace, self.refill, self.zone_nets):
                v.Add(c, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
            v.Add(self.log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
            h = wx.BoxSizer(wx.HORIZONTAL)
            h.AddStretchSpacer(); h.Add(self.run_btn, 0, wx.RIGHT, 8); h.Add(self.close_btn, 0)
            v.Add(h, 0, wx.EXPAND | wx.ALL, 10)
            p.SetSizer(v)
            outer = wx.BoxSizer(wx.VERTICAL); outer.Add(p, 1, wx.EXPAND)
            self.SetSizerAndFit(outer)
            self.SetMinSize(self.GetSize())
            try:
                self.SetIcon(wx.Icon(os.path.join(HERE, 'icon.png'), wx.BITMAP_TYPE_PNG))
            except Exception:
                pass
            self.kicad = connect(self.say)
            if self.kicad is None:
                self.run_btn.Disable()

        def say(self, msg):
            wx.CallAfter(self.log.AppendText, msg + '\n')

        def settings(self) -> Settings:
            st = Settings()
            st.engine = ['shape', 'grid'][self.engine.GetSelection()]
            st.angles = ['45', 'any', '90'][self.angles.GetSelection()]
            st.pitch = float(self.pitch.GetValue()); st.passes = int(self.passes.GetValue())
            st.via_cost = float(self.via_cost.GetValue()); st.bend_cost = float(self.bend_cost.GetValue())
            st.nets = self.nets.GetValue(); st.exclude = self.exclude.GetValue(); st.layer_costs = self.layer_costs.GetValue()
            st.replace = self.replace.GetValue(); st.refill = self.refill.GetValue(); st.zone_nets = self.zone_nets.GetValue()
            return st

        def on_run(self, _evt):
            try:
                st = self.settings()
            except ValueError as e:
                wx.MessageBox(f'Check the numbers: {e}', 'Tracklayer', wx.ICON_WARNING)
                return
            self.run_btn.Disable(); self.close_btn.Disable()
            threading.Thread(target=self._work, args=(st,), daemon=True).start()

        def _work(self, st):
            try:
                run_routing(self.kicad, st, self.say)
            except Exception as e:
                import traceback
                self.say('Failed: ' + ''.join(traceback.format_exception(type(e), e, e.__traceback__)))
            finally:
                wx.CallAfter(self.run_btn.Enable); wx.CallAfter(self.close_btn.Enable)

    app = wx.App(False)
    dlg = Dialog()
    if os.environ.get('TRACKLAYER_AUTORUN'):      # test harness: route, print the log, close when done
        def poll():
            text = dlg.log.GetValue()
            if 'Done.' in text or 'Failed' in text:
                print(text)
                wx.CallLater(int(os.environ.get('TRACKLAYER_AUTORUN_HOLD_MS', '0')) + 1, dlg.EndModal, 0)
            else:
                wx.CallLater(500, poll)
        wx.CallLater(800, dlg.on_run, None)
        wx.CallLater(1300, poll)
    dlg.ShowModal()
    dlg.Destroy()


# ----------------------------------------------------------------------------- tkinter window

def main_tk():
    import tkinter as tk
    from tkinter import ttk

    class App:
        def __init__(self, root):
            self.root = root
            root.title('Tracklayer autoroute')
            root.minsize(620, 540)
            f = ttk.Frame(root, padding=10)
            f.pack(fill='both', expand=True)

            def field(row, col, label, value):
                ttk.Label(f, text=label).grid(row=row, column=col * 2, sticky='w', padx=(0, 6), pady=3)
                v = tk.StringVar(value=value)
                ttk.Entry(f, textvariable=v, width=14).grid(row=row, column=col * 2 + 1, sticky='ew', pady=3)
                return v

            self.engine = tk.StringVar(value='shape')
            ttk.Label(f, text='Engine').grid(row=0, column=0, sticky='w')
            ttk.Combobox(f, textvariable=self.engine, values=['shape', 'grid'], state='readonly', width=12).grid(row=0, column=1, sticky='ew', pady=3)
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
            self.replace = tk.BooleanVar(value=False); self.refill = tk.BooleanVar(value=True); self.zone_nets = tk.BooleanVar(value=True)
            ttk.Checkbutton(f, text='Remove existing unlocked tracks of the routed nets first', variable=self.replace).grid(row=5, column=0, columnspan=4, sticky='w')
            ttk.Checkbutton(f, text='Refill zones afterwards', variable=self.refill).grid(row=6, column=0, columnspan=4, sticky='w')
            ttk.Checkbutton(f, text='Route pads of pour nets that lie outside their pour', variable=self.zone_nets).grid(row=7, column=0, columnspan=4, sticky='w')
            self.log = tk.Text(f, height=16, width=80, state='disabled', font=('Consolas', 9))
            self.log.grid(row=8, column=0, columnspan=4, sticky='nsew', pady=(8, 8))
            sb = ttk.Scrollbar(f, command=self.log.yview); sb.grid(row=8, column=4, sticky='ns')
            self.log['yscrollcommand'] = sb.set
            bar = ttk.Frame(f); bar.grid(row=9, column=0, columnspan=4, sticky='e')
            self.run_btn = ttk.Button(bar, text='Route', command=self.on_run); self.run_btn.pack(side='left', padx=(0, 8))
            self.close_btn = ttk.Button(bar, text='Close', command=root.destroy); self.close_btn.pack(side='left')
            f.columnconfigure(1, weight=1); f.columnconfigure(3, weight=1); f.rowconfigure(8, weight=1)
            self.kicad = connect(self.say)
            if self.kicad is None:
                self.run_btn['state'] = 'disabled'

        def say(self, msg):
            def _do():
                self.log['state'] = 'normal'; self.log.insert('end', msg + '\n'); self.log.see('end'); self.log['state'] = 'disabled'
            self.root.after(0, _do)

        def settings(self) -> Settings:
            st = Settings()
            st.engine = self.engine.get(); st.angles = self.angles.get()
            st.pitch = float(self.pitch.get()); st.passes = int(self.passes.get())
            st.via_cost = float(self.via_cost.get()); st.bend_cost = float(self.bend_cost.get())
            st.nets = self.nets.get(); st.exclude = self.exclude.get(); st.layer_costs = self.layer_costs.get()
            st.replace = self.replace.get(); st.refill = self.refill.get(); st.zone_nets = self.zone_nets.get()
            return st

        def on_run(self):
            try:
                st = self.settings()
            except ValueError as e:
                self.say(f'Check the numbers: {e}')
                return
            self.run_btn['state'] = 'disabled'; self.close_btn['state'] = 'disabled'
            threading.Thread(target=self._work, args=(st,), daemon=True).start()

        def _work(self, st):
            try:
                run_routing(self.kicad, st, self.say)
            except Exception as e:
                import traceback
                self.say('Failed: ' + ''.join(traceback.format_exception(type(e), e, e.__traceback__)))
            finally:
                self.root.after(0, lambda: (self.run_btn.config(state='normal'), self.close_btn.config(state='normal')))

    root = tk.Tk()
    app = App(root)
    if os.environ.get('TRACKLAYER_AUTORUN'):
        def poll():
            text = app.log.get('1.0', 'end')
            if 'Done.' in text or 'Failed' in text:
                print(text)
                root.after(int(os.environ.get('TRACKLAYER_AUTORUN_HOLD_MS', '0')), root.destroy)
            else:
                root.after(500, poll)
        root.after(1000, app.on_run)
        root.after(1500, poll)
    root.mainloop()


# ----------------------------------------------------------------------------- no toolkit at all

def main_plain():
    lines = []
    kicad = connect(lines.append)
    if kicad is not None:
        try:
            run_routing(kicad, Settings(), lines.append)
        except Exception as e:
            lines.append(f'Failed: {e}')
    text = '\n'.join(lines)
    print(text)
    if sys.platform.startswith('win'):
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, 'Tracklayer autoroute', 0x40)


def main():
    try:
        import wx  # noqa: F401
        main_wx()
        return
    except ImportError:
        pass
    try:
        import tkinter  # noqa: F401
        main_tk()
        return
    except ImportError:
        pass
    main_plain()


if __name__ == '__main__':
    main()
