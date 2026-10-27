# SchematicNetlist

Turn a schematic PDF into a netlist — components, nets, an **HSPICE** deck and an **Excel**
analysis report. One Windows app, nothing to install; also a Python package with a CLI.

[**⬇ Download SchematicNetlist.exe (latest release)**](../../releases/latest)

> The result is derived from a drawing, not from the CAD database. Review the *Warnings*
> sheet and the engine log before relying on it.

---

## Quick start

1. Download `SchematicNetlist.exe` and double-click it (first start: *More info → Run anyway*,
   the file is not code-signed).
2. Drop your schematic PDF on the window (or **Browse...**).
3. Check the **Project name** and the outputs you want, click **Analyze schematic**, then
   **Open output folder**.

Results go to a folder **named after the PDF, next to it** (`Board.pdf` → `Board\`):

| File | Content |
|---|---|
| `<name>_analysis.xlsx` | **Summary** (counts, parts by kind, log), **Components** (refdes, kind, value/part, pins, pages), **Pins** (every pin with its net), **Nets** (name, rail, aliases, connections), **Warnings** |
| `<name>.sp` | HSPICE netlist — editable deck with stub models, stub subcircuits and `.PARAM`s for values the drawing did not give |
| `<name>_netlist.txt` / `.json` | flat connectivity netlist, human-readable and machine-readable |
| `<name>_engine_log.txt` | what the engine found and what it could not resolve |

## What it reads

The engine works on the **vector drawing** of the PDF, the way every ECAD tool exports it:

* orthogonal line segments are wires (a wire placed a hair off-grid, under 2° of slant, is
  still a wire); they join at shared endpoints and at T-junctions that carry a junction dot (a
  pin ending on a symbol edge has no dot and is *not* a connection; pin-direction arrowheads on
  a box edge are not dots);
* curves, diagonals, triangles and boxes are symbol bodies; a wire ending on one is a pin.
  Graphics drawn *inside* a box symbol (a coil in its rectangle, an internal switch diagram,
  polarity marks) are body, never wires; a small "×" at a wire end is a no-connect mark;
* reference designators, pin numbers, pin names (inside boxes) and net labels are placed by
  position, rotated text included; equal net labels join nets across pages. When the drawing
  prints designators in their own colour, free-form names in that colour (`R_SHUNT_1`,
  `J1PWR`, `TP_REF_3`) count as designators too;
* a box with several pins and a title at its top-left corner, or with numbered pins, is a
  hierarchical **block** (sheet symbol) even without a designator;
* **scanned / image-only pages** are read with OpenCV + Tesseract OCR into the same model
  (less reliable — the log lists those pages).

On top of that, `circuit.py` adds the circuit meaning:

| | Rule |
|---|---|
| Part kind | refdes prefix (`R`, `C`, `L`, `D`, `Q`, `M`, `U`, `K`, `J`, `SW`, `F`, `TP`, `Y`, …), confirmed or overridden by the drawn shape (slanted blade = switch/relay contact, arcs only = coil, zigzag = resistor, triangle = diode / solid-state relay input). A boxed unit of a relay whose other units are contacts is its coil. Boxes without a known prefix are blocks |
| Value / part | nearest free text that looks like a value (`10k`, `4k7`, `100nF`) for passives; nearest part-number-like text for everything else |
| Net name | the net label; with several aliases the most used one (rail names win); unlabeled nets are `N<page>_<n>` |
| Power / ground | by name (`GND`, `VCC`, `VDD`, `+5V`, `3V3`, …) or, unnamed, by being ≥ 5× bigger than the document's 99th-percentile net (≥ 50 pins) |
| Warnings | unnamed connector flags, single-pin nets, box pins without numbers (info when the symbol prints none at all), a pin on two nets, relay contacts without a coil, nets with conflicting labels |

### HSPICE mapping

| Part kind | Element |
|---|---|
| resistor, fuse, jumper, switch, relay coil | `R` (`.PARAM` when no value; switch `1m` = closed). A boxed coil with extra pins: `R` on its two lowest-numbered pins |
| capacitor / inductor, ferrite bead | `C` / `L` |
| diode, BJT, MOSFET | `D`, `Q`, `M` with a stub `.MODEL`; pin order = pin numbers (comment says so — check it) |
| relay contact | `S` switch controlled by the coil nodes of the same relay (`K5_1A` contact ↔ `K5_1C` coil) |
| source / battery | `V` |
| IC, blocks, networks, crystals, transformers, odd pin counts | `X` instance of an empty `.SUBCKT` whose pins are documented |
| connector, test point, fiducial, mechanical | comment lines (`pin = node`) |

Nets named `GND`/`0V` become node `0`; other rails go to `.GLOBAL`.

## Requirements

Windows 10/11 64-bit for the exe (no Python, no admin). The PDF must be in a writable folder.

## Limitations

* Geometry rules expect orthogonal wires and junction dots at T-junctions (every ECAD export
  does this); hand-drawn or heavily stylised sheets will lose connections.
* Values are read from text near the symbol — a crowded drawing can attach the wrong one.
* Pin roles of semiconductors (A/K, C/B/E, D/G/S) are not known from a drawing; the deck
  keeps pin-number order and says so in a comment.
* Scanned pages: expect roughly half the labels of a clean vector page; check them by hand.

---

## For developers

```powershell
uv venv
uv pip install -e ".[image,gui,dev]"      # image = OCR for scanned pages, gui = drag-and-drop, dev = PyInstaller
winget install UB-Mannheim.TesseractOCR   # OCR engine (found in its default folder even if not on PATH)
.venv\Scripts\schematic-netlist.exe doctor
```

```powershell
schematic-netlist analyze MySchematic.pdf [-o outdir] [--name MyBoard] [--pages 1-20] [--no-excel] [--no-spice] [--no-netlist] [--workers 4] [--no-cache]
schematic-netlist-gui
```

Pages are read in parallel processes and the extracted netlist is cached in
`%LOCALAPPDATA%\SchematicNetlist\cache` (keyed by PDF content + engine code), so a re-run of
the same PDF takes seconds.

Build the exe (bundles Tesseract, ~110 MB): `.\build.ps1` (first time `.\build.ps1 -Setup` creates `.venv`) -> `.\SchematicNetlist.exe`.
Headless use of the exe: `SchematicNetlist.exe --cli analyze board.pdf`.

### Layout

| Module | Role |
|---|---|
| `netlist.py` | drawing primitives → wires, symbols, pins, labels, cross-page nets (`Design`) |
| `image_extract.py` | scanned pages: OpenCV + Tesseract → the same primitives |
| `circuit.py` | `Design` → typed parts, named nets, rails, warnings (`Circuit`) |
| `spice.py`, `netlist_export.py`, `report_excel.py` | writers |
| `analyze.py` | one call PDF → all files (used by `cli.py` and `gui.py`) |

License: MIT.
