# Tracklayer

A grid autorouter for KiCad boards. Point it at a `.kicad_pcb`, it lays tracks and vias for
the unrouted nets and writes a copy of the board back with the routes added. Nothing else in
the file is touched: footprints, zones, text and the tracks you routed by hand stay exactly as
they were.

```
pip install .
tracklayer my-board.kicad_pcb --svg my-board.svg
```

That writes `my-board-routed.kicad_pcb` and a picture of the result. Open the routed board in
KiCad, refill the zones (B) and run DRC.

Tracklayer is pure Python with numpy. It does not need KiCad installed, so it runs anywhere,
including CI. It reads KiCad 7, 8, 9 and 10 board files, and the net classes from the project's
`.kicad_pro`.

## Two engines

**Shape** (the default) routes on exact geometry. Every piece of copper that is not the net
being routed becomes a polygon grown by the clearance plus half the track width, and tracks run
in straight lines from corner to corner of those polygons, so clearances are met exactly (to a
few microns, never a grid's worth) and tight escapes from fine-pitch pads just work. Tracks come
out at any angle. Vias sit on a fine lattice of spots that are legal on every layer.

**Grid** (`--engine grid`) is a raster A* with 45-degree output. It is faster on big sparse
boards and gives the classic look, at the price of a clearance margin tied to the grid pitch.

Both route every copper layer in the board's stack with through vias: two, four, six, as many
as the board has. Blind and buried vias are not supported.

## What it does

* **Net classes are honoured.** Track width, clearance, via size and drill come from each net's
  class in the `.kicad_pro`. `--width`, `--clearance`, `--via`, `--drill` override them for a run.
* **Existing copper is respected.** Pads, hand-routed tracks, vias, keep-out areas and copper
  graphics are obstacles with the right clearance. Tracks and vias already on a net count as
  part of it, so you can hand-route the critical nets first, lock them, and let Tracklayer do
  the rest. `--replace` removes the unlocked tracks of the routed nets first.
* **Pours are understood.** A pad that sits inside a pour of its own net is taken as connected.
  Pads of a pour net that lie outside it are routed to the nearest copper of that net, or to a
  point deep inside the pour, with a via if the pour is on another layer. Other nets' tracks
  may cross a pour, at a cost that is small for a plane-sized pour and high for a small one, so
  the local copper islands you drew are left whole where possible.
* **Rip-up and reroute.** Pass one routes every net with earlier routes as hard obstacles. Nets
  that fail are rerouted in later passes with collisions allowed at a rising price, so the nets
  in the way get moved. Anything still colliding at the end is dropped and reported rather than
  written out wrong.
* **Clean geometry.** Routes are smoothed into 45-degree segments (`--angles any` for
  free-angle, `--angles 90` for orthogonal). Track ends land on pad centres.
* **Neck-down.** When a wide net cannot leave a small pad at its full width without breaking
  clearance, that connection is laid at the Default class width instead (`--neck` to choose).
* **Any number of copper layers** with through vias. Multilayer boards are routed on every
  copper layer in the stack, or on the ones you name with `--layers F.Cu,B.Cu`.

## Inside KiCad

There is a plugin for the PCB editor: a dialog with the main options, a live log, and the
result added straight to the open board as one change (so Undo takes it back), with the zones
refilled afterwards. It comes in two flavours in the same package: the **API plugin** for
KiCad 9 and later (the open board is fetched and the tracks pushed back over KiCad's IPC API;
enable it once under Preferences > Plugins > Enable KiCad API, and the first run sets up its
Python environment), and the **classic plugin** for KiCad 8 to 10, which the KiCad team is
retiring in version 11. On 9 and 10 you will see both entries; use the API one. Install it with

```
python install_kicad_plugin.py
```

which copies the plugin and the router into KiCad's user plugin folder for every KiCad version
it finds under Documents/KiCad (pass `--dir` for another folder). Or take the package zip from
the [releases page](https://github.com/MichaelGaylor/Tracklayer/releases) and use
Plugin and Content Manager > Install from File. (`pcm/SUBMIT.md` has the steps for the PCM listing.) Then in the PCB editor use
Tools > External Plugins > Tracklayer autoroute, or the toolbar button. Hand-route and lock
anything critical first; the plugin keeps existing tracks and routes the rest.

## Options worth knowing

| Option | Default | What it does |
| --- | --- | --- |
| `--engine` | shape | `shape` or `grid`, see above. |
| `--pitch` | 0.2 mm | Grid engine: grid pitch. Smaller is slower and finds narrower gaps. |
| `--via-pitch` | 0.25 mm | Shape engine: spacing of the candidate via spots. |
| `--passes` | 8 | Rip-up and reroute passes. |
| `--via-cost` | 4 mm | How much track a via is worth. Raise it for fewer vias. |
| `--bend-cost` | 0.3 mm | Price of a direction change. |
| `--zone-cost` | 0.3 | Extra cost factor for crossing a plane-sized pour of another net. |
| `--zone-cost-local` | 2.5 | The same for a small pour. |
| `--nets A B*` | all | Route only nets matching the patterns. `--exclude` does the opposite. |
| `--no-zone-nets` | | Do not route nets that have pours at all. |
| `--pour-obstacle FRAC` | off | Pours covering less than FRAC of the board are hard obstacles for other nets. |
| `--via-in-pad` | | Allow vias inside SMD pads of the same net. |
| `--in-place` | | Write the routes into the input file instead of a copy. |

## How the grid engine works

The board is rasterised onto a square grid, one plane per copper layer. For every route class
(track width, clearance, via size) each cell records which net's copper, grown by the clearance
a track of that class needs, covers it. A track of net *n* may use a cell only if nobody else
does. Each net is then routed as a tree: its connected pieces of copper are joined one at a time
by a multi-source A* search with eight neighbours per layer and vias between layers. Finished
routes are smoothed, checked again against the same rasters, and painted back in as obstacles.

In the grid engine, because the check is done at cell centres, every clearance carries a small safety margin of
0.71 times the grid pitch (0.14 mm at the default pitch). That makes the result conservative:
KiCad's DRC should never complain, but a gap only just wide enough for a track will be refused
at the default pitch. Use `--pitch 0.1` for boards with SOIC or finer pitch parts, or when a
net class asks for a clearance much larger than the gaps between the pads it has to reach; it
takes about four times longer. `--margin` sets the margin directly if you know what you are doing.

A note on net classes: KiCad applies the larger of the two nets' clearances between any two
items. A class with a 0.4 mm clearance can therefore never be routed into a 2.54 mm header pin
whose neighbours are 0.84 mm away, by this router or by hand, because the track would sit
closer than 0.4 mm to the next pin. Tracklayer reports such nets by pad name instead of
guessing.

## Limits, honestly

This is a grid maze router, not a shape-based push-and-shove engine. It will route most
two-layer hobby and small-production boards cleanly and quickly, and it leaves the hard nets
for you to do by hand rather than guessing. It does not do differential pairs, length matching,
blind or buried vias, or custom-shaped pads (these are treated as their bounding rectangle).
It has been used on a 200 x 130 mm two-layer power board with 55 nets, which routes in about
a minute and a half with zero DRC violations.

## Development

```
python -m pytest tests
python -m tracklayer path/to/board.kicad_pcb --svg out.svg
```

The tests build tiny boards in KiCad's own text format and check clearances on the result.

## About

Tracklayer is made by [Tiggy Engineering](https://www.tiggyengineering.com), Mike Gaylor's
workshop: CNC, electronics and the odd EDM tap burner. It is free and MIT licensed. If it saves
you an evening, have a look at what we make and sell on the website, or [buy us a beer](https://www.tiggyengineering.com).
Bug reports and boards it gets wrong are welcome in the issues.
