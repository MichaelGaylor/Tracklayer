"""The Tracklayer dialog inside KiCad's PCB editor.

Flow: the open board is written to a temporary file (unsaved edits included), Tracklayer routes
that file in a worker thread while the dialog shows its log, and the resulting segments and vias
are then added to the live board through the pcbnew API, so the result is one undoable, visible
change. Zones are refilled afterwards if asked.
"""
import os
import sys
import tempfile
import threading

import pcbnew
import wx

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _tracklayer():
    import tracklayer  # noqa: F401  (the copy installed next to this file, or the checkout)
    from tracklayer import __version__
    from tracklayer.board import load, match_nets
    from tracklayer.router import Options, Router
    return __version__, load, match_nets, Options, Router


def _netclasses_from_board(board, b):
    """Copy the net classes KiCad has resolved onto Tracklayer's board model, so a temporary file
    without a .kicad_pro next to it still gets the right widths and clearances."""
    from tracklayer.board import NetClass
    ncs = board.GetAllNetClasses()
    for name, c in ncs.items():
        b.netclasses[str(name)] = NetClass(str(name), pcbnew.ToMM(c.GetClearance()), pcbnew.ToMM(c.GetTrackWidth()),
                                           pcbnew.ToMM(c.GetViaDiameter()), pcbnew.ToMM(c.GetViaDrill()))
    for code, net in board.GetNetInfo().NetsByNetcode().items():
        if not code:
            continue
        mine = b.net_code(net.GetNetname())
        if mine is not None:
            b.net_class_of[mine] = str(net.GetNetClassName())
    ds = board.GetDesignSettings()
    b.min_clearance = pcbnew.ToMM(ds.m_MinClearance)
    b.edge_clearance = pcbnew.ToMM(ds.m_CopperEdgeClearance)
    b.hole_clearance = pcbnew.ToMM(ds.m_HoleClearance)


def apply_routes(board, b, routes):
    """Add Tracklayer's routes to a live pcbnew board. Returns (segments, vias) added."""
    ns, nv = 0, 0
    for r in routes.values():
        net = board.FindNet(b.net_name(r.net))
        if net is None:
            continue
        for layer, x1, y1, x2, y2, w in r.segments:
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I_MM(x1, y1)); t.SetEnd(pcbnew.VECTOR2I_MM(x2, y2))
            t.SetWidth(pcbnew.FromMM(w)); t.SetLayer(board.GetLayerID(layer)); t.SetNet(net)
            board.Add(t); ns += 1
        _, _, vd, vdr = r.params
        l1, l2 = r.via_layers or ('F.Cu', 'B.Cu')
        for x, y in r.vias:
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I_MM(x, y)); v.SetWidth(pcbnew.FromMM(vd)); v.SetDrill(pcbnew.FromMM(vdr))
            v.SetLayerPair(board.GetLayerID(l1), board.GetLayerID(l2)); v.SetNet(net)
            board.Add(v); nv += 1
    return ns, nv


class TracklayerDialog(wx.Dialog):
    def __init__(self, parent, board):
        super().__init__(parent, title='Tracklayer autoroute', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.result = None
        self.worker = None
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

        self.pitch = field('Grid pitch (mm)', '0.2', 'Grid engine only: smaller finds narrower gaps and is slower. Shape engine: via lattice pitch.')
        self.passes = field('Rip-up passes', '8')
        self.via_cost = field('Via cost (mm of track)', '4', 'Raise for fewer vias')
        self.bend_cost = field('Bend cost (mm)', '0.3')
        self.nets = field('Only nets (patterns)', '', 'Space separated, e.g. D* CLK; empty routes everything unrouted')
        self.exclude = field('Skip nets (patterns)', '')
        grid.Add(wx.StaticText(p, label='Engine'), 0, wx.ALIGN_CENTER_VERTICAL)
        self.engine = wx.Choice(p, choices=['shape (exact geometry, any angle)', 'grid (45 degree tracks)'])
        self.engine.SetSelection(0)
        grid.Add(self.engine, 1, wx.EXPAND)
        grid.Add(wx.StaticText(p, label='Corners (grid engine)'), 0, wx.ALIGN_CENTER_VERTICAL)
        self.angles = wx.Choice(p, choices=['45 degrees', 'any angle', '90 degrees'])
        self.angles.SetSelection(0)
        grid.Add(self.angles, 1, wx.EXPAND)
        grid.Add(wx.StaticText(p, label='Layers'), 0, wx.ALIGN_CENTER_VERTICAL)
        copper = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
        self.layers = wx.TextCtrl(p, value=','.join(copper))
        grid.Add(self.layers, 1, wx.EXPAND)

        self.replace = wx.CheckBox(p, label='Remove existing unlocked tracks of the routed nets first')
        self.refill = wx.CheckBox(p, label='Refill zones afterwards'); self.refill.SetValue(True)
        self.zone_nets = wx.CheckBox(p, label='Route pads of pour nets that lie outside their pour'); self.zone_nets.SetValue(True)

        self.log = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(560, 220))
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
            version = _tracklayer()[0]
            self.say(f'Tracklayer {version} by Tiggy Engineering  -  www.tiggyengineering.com')
            self.say(f'Board: {os.path.basename(board.GetFileName()) or "(unsaved)"}')
        except Exception as e:
            self.say(f'Tracklayer could not be imported: {e}')
            self.run_btn.Disable()

    def say(self, msg):
        self.log.AppendText(msg + '\n')

    def _opts(self):
        _, _, _, Options, _ = _tracklayer()
        angles = ['45', 'any', '90'][self.angles.GetSelection()]
        layers = [l.strip() for l in self.layers.GetValue().split(',') if l.strip()]
        engine = ['shape', 'grid'][self.engine.GetSelection()]
        return Options(engine=engine, via_pitch=float(self.pitch.GetValue()), pitch=float(self.pitch.GetValue()), passes=int(self.passes.GetValue()),
                       via_cost=float(self.via_cost.GetValue()), bend_cost=float(self.bend_cost.GetValue()),
                       angles=angles, layers=layers or None, route_zone_nets=self.zone_nets.GetValue(), verbose=False)

    def on_run(self, _evt):
        try:
            opts = self._opts()
        except ValueError as e:
            wx.MessageBox(f'Check the numbers: {e}', 'Tracklayer', wx.ICON_WARNING)
            return
        self.run_btn.Disable(); self.close_btn.Disable()
        self.log.Clear()
        self.say('Writing the board to a temporary file...')
        tmpdir = tempfile.mkdtemp(prefix='tracklayer-')
        path = os.path.join(tmpdir, 'board.kicad_pcb')
        try:
            pcbnew.SaveBoard(path, self.board, True)
        except TypeError:
            pcbnew.SaveBoard(path, self.board)
        nets_pat = self.nets.GetValue().split()
        excl_pat = self.exclude.GetValue().split()
        replace = self.replace.GetValue()
        self.worker = threading.Thread(target=self._route, args=(path, opts, nets_pat, excl_pat, replace), daemon=True)
        self.worker.start()

    def _route(self, path, opts, nets_pat, excl_pat, replace):
        try:
            _, load, match_nets, Options, Router = _tracklayer()
            b = load(path)
            _netclasses_from_board(self.board, b)
            all_nets = {c for c in b.nets if c > 0}
            nets = match_nets(b, nets_pat) if nets_pat else set(all_nets)
            if excl_pat:
                nets -= match_nets(b, excl_pat)
            counts = {}
            for p in b.pads:
                counts[p.net] = counts.get(p.net, 0) + 1
            nets = {n for n in nets if counts.get(n, 0) >= 2 or any(z.net == n and not z.is_keepout for z in b.zones)}
            if replace:
                b.tracks = [t for t in b.tracks if not (t.net in nets and not t.locked)]
                b.vias = [v for v in b.vias if not (v.net in nets and not v.locked)]
            wx.CallAfter(self.say, f'{len(b.pads)} pads, {len(nets)} nets to route, {len(b.tracks)} existing tracks kept')
            if opts.engine == 'shape':
                from tracklayer.shape_router import ShapeRouter
                r = ShapeRouter(b, nets, opts)
            else:
                r = Router(b, nets, opts)
            orig_say = r.say

            def say(*a):
                orig_say(*a)
                wx.CallAfter(self.say, ' '.join(str(x) for x in a))
            r.say = say
            r.run()
            wx.CallAfter(self._finished, b, r, nets if replace else None)
        except Exception as e:
            import traceback
            wx.CallAfter(self.say, 'Failed: ' + ''.join(traceback.format_exception(type(e), e, e.__traceback__)))
            wx.CallAfter(self._enable)

    def _enable(self):
        self.run_btn.Enable(); self.close_btn.Enable()

    def _finished(self, b, r, replace_nets):
        board = self.board
        removed = 0
        if replace_nets:
            names = {b.net_name(n) for n in replace_nets}
            for t in list(board.GetTracks()):
                if t.GetNetname() in names and not t.IsLocked():
                    board.Remove(t); removed += 1
        ns, nv = apply_routes(board, b, r.routes)
        self.say(f'Added {ns} segments and {nv} vias to the board' + (f', removed {removed} old tracks' if removed else ''))
        if self.refill.GetValue():
            self.say('Refilling zones...')
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        pcbnew.Refresh()
        if r.unrouted:
            self.say('Left for hand routing: ' + ', '.join(b.net_name(n) for n in r.unrouted))
        self.say('Done. Run DRC (Inspect > Design Rules Checker) to confirm.')
        self._enable()


class TracklayerAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'Tracklayer autoroute (classic)'
        self.category = 'Routing'
        self.description = 'Route the unrouted nets of this board with Tracklayer'
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(HERE, 'icon.png')
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        board = pcbnew.GetBoard()
        dlg = TracklayerDialog(None, board)
        dlg.ShowModal()
        dlg.Destroy()
