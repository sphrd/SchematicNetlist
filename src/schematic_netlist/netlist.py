"""Netlist extraction from schematic PDFs (vector drawing; scanned pages via `image_extract`).

Turns the drawing primitives of each page into a circuit model:

* **Wires** -- orthogonal line segments. Two segments are connected when their
  endpoints coincide, when they overlap colinearly, or at a T-junction that is
  marked with a junction dot (ECAD tools always draw a dot at a real T; a pin
  ending on a body edge has no dot and is therefore *not* a connection).
* **Symbols** -- curves, diagonal lines, solid rectangles and small "internal"
  line groups, clustered by touch. A wire endpoint that dangles onto a symbol
  is a **terminal** (a pin) of that symbol.
* **Components** -- symbol clusters that own a reference designator (R12,
  C3, U1-2, K5_2A, J7 ...). Terminals get their pin number (text just beside the
  pin line) and, for box symbols, their pin name (text inside the box).
* **Net labels** -- symbol clusters *without* a refdes are off-page / power /
  ground connectors; the text on their far side is the net name. Every net
  label is global: equal names join nets across pages.

Nothing here depends on colours, so it generalises to other schematics drawn
the same way (orthogonal wires, dotted T-junctions, text-labelled connectors).
"""
from __future__ import annotations

import hashlib
import math
import os
import pickle
import re
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Every geometric tolerance is relative to the page's own drawing: schematic CAD tools size text,
# pin spacing and symbols together, so the height of the page's legible text is the unit. The
# constants below are written in PDF points for TEXT_UNIT-pt text; build_page() multiplies them by
# Primitives.scale = (this page's text height) / TEXT_UNIT. E.g. TOL = 0.085 text heights.
TEXT_UNIT = 4.1
LEGIBLE_MIN = 0.4  # text under this fraction of the page's large text is hidden/annotation, not drawing text
TEXT_MIN = 0.6  # text under this fraction of the page's large text does not set the page's text height
TOL = 0.35  # geometric coincidence tolerance (pt)
DOT_MAX = 1.8  # max size of a junction dot (pt)
DOT_FILL_MAX = 3.5  # max size of a junction dot drawn as a filled round blob (pt)
THIN_MAX = 1.0  # a rectangle at most this thick (and 4x as long) is a thick stroke, not a box (pt)
INTERNAL_MAX = 16.0  # max extent of a line group that is part of a symbol body (pt)
TINY_NET = 3.0  # a line group this small with both ends on symbol graphics is symbol body (pt)
PIN_GAP = 3.0  # max gap between a pin line's loose end and its symbol body (pt)
PART_GAP = 2.5  # anonymous symbol pieces this close to each other form one part (pt)
JOIN_GAP = 4.0  # an anonymous piece this close to a refdes'd symbol belongs to it (pt)
FAR_LABEL_MAX = 60.0  # max gap between an off-page connector symbol and its net-name text (pt)
FAR_LABEL_NEAR = 30.0  # beyond this gap the text needs a clear line of sight to the symbol (pt)
FAR_LABEL_OWN = 10.0  # text this close to another flag's wire end belongs to that flag, never to a far one (pt)
LOCAL_SPAN = 25.0  # max extent of one connector symbol along its pin direction (pt)
PIN_RUN_MAX = 10.0  # a pin number sits within this distance of the pin's end, along the pin line (pt)
NESTED_BOX_MIN = 3.0  # a box holding another box this big is a grouping frame, not a symbol (pt)
REFDES_REACH = 35.0  # max gap between a large symbol and its designator when no nearer one claims it (pt)

REFDES_RE = re.compile(r"^[A-Z]{1,3}\d{1,4}[A-Z]?(?:[_-]\d{1,3}[A-Z]?)?$")
# refdes with a word-like unit suffix (J1PWR, U3CTRL); accepted only in the document's refdes colour
# because pin names (S06IN, D2OUT) look the same
REFDES_LONG_RE = re.compile(r"^[A-Z]{1,3}\d{1,4}[A-Z]{2,4}$")
# designator spelled in mixed case (Rload1, Vin1): a capital, a short word, a number
REFDES_MIXED_RE = re.compile(r"^[A-Z](?=[A-Za-z]*[a-z])[A-Za-z]{1,6}\d{1,3}$")
# free-form designators (R_SHUNT_1, SOCKET2, TP_A_1): letters, digits and underscores with at
# least one digit; accepted only in the document's refdes colour
REFDES_LOOSE_RE = re.compile(r"^(?=.*\d)[A-Z][A-Z0-9]{0,7}(?:_[A-Z0-9]{1,6}){0,2}$")
# refdes with an instance suffix (K5_2A: part K5, instance 2, unit A) -- multi-channel/multi-copy designs
SUFFIXED_REFDES_RE = re.compile(r"^[A-Z]{1,3}\d{1,4}[A-Z]?_\d{1,3}[A-Z]?$")
PINNUM_RE = re.compile(r"^(?:[A-Z]{0,2}\d{1,3}[A-Z]?|[A-Z]{1,2})$")
# a number with a unit prefix (1M, 10K, 4k7, 100n): the value of a discrete part, never its pin number
VALUE_UNIT_RE = re.compile(r"^\d+(?:\.\d+)?[pnuµmkKMG][FHΩ]?$|^\d+[RKMpnu]\d+[A-Za-z]*$")
# component values / part attributes -- never net names
VALUE_RE = re.compile(
    r"^(?:NOB|DNP|DNF|\d{1,4}|\d+(?:\.\d+)?[pnuµmkKMG][FHΩ]?|\d+(?:\.\d+)?[FHΩVW]|\d+/\d+W|"
    r"\d+[RKMpnu]\d+[A-Za-z]*|\d+(?:\.\d+)?%)$"
)


def _refdes_like(w: "Word", refdes_color: tuple) -> bool:
    """A reference designator by pattern, or -- in the document's refdes colour -- by looser shape."""
    if REFDES_RE.match(w.text):
        return True
    if not refdes_color and REFDES_MIXED_RE.match(w.text):
        return True
    if not refdes_color or w.color != refdes_color:
        return False
    if VALUE_RE.match(w.text) or PINNUM_RE.match(w.text):
        return False
    return bool(REFDES_LONG_RE.match(w.text) or REFDES_LOOSE_RE.match(w.text))


# --------------------------------------------------------------------------- helpers
class UnionFind:
    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, a):
        p = self.parent
        if a not in p:
            p[a] = a
            return a
        root = a
        while p[root] != root:
            root = p[root]
        while p[a] != root:
            p[a], a = root, p[a]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class Grid:
    """Tiny uniform-grid spatial index over bounding boxes."""

    def __init__(self, cell: float = 6.0) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    def add(self, idx: int, x0: float, y0: float, x1: float, y1: float) -> None:
        c = self.cell
        for gx in range(int(x0 // c), int(x1 // c) + 1):
            for gy in range(int(y0 // c), int(y1 // c) + 1):
                self.cells[(gx, gy)].append(idx)

    def query(self, x0: float, y0: float, x1: float, y1: float) -> set[int]:
        c = self.cell
        out: set[int] = set()
        for gx in range(int(x0 // c), int(x1 // c) + 1):
            for gy in range(int(y0 // c), int(y1 // c) + 1):
                out.update(self.cells.get((gx, gy), ()))
        return out


def _pt_seg_dist(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    if L2 <= 1e-12:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / L2))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _pt_bbox_dist(px: float, py: float, b: tuple[float, float, float, float]) -> float:
    dx = max(b[0] - px, 0.0, px - b[2])
    dy = max(b[1] - py, 0.0, py - b[3])
    return math.hypot(dx, dy)


def _bbox_gap(a: tuple, b: tuple) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- model
@dataclass
class Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    color: tuple = ()
    role: str = ""  # "refdes" | "pin" | "pinname" | "label" | ""

    @property
    def vertical(self) -> bool:
        """Rotated text (reads bottom-to-top or top-to-bottom): taller than wide."""
        return len(self.text) > 1 and (self.bottom - self.top) > 1.5 * (self.x1 - self.x0)

    @property
    def text_height(self) -> float:
        return (self.x1 - self.x0) if self.vertical else (self.bottom - self.top)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass
class Terminal:
    page: int
    x: float
    y: float
    seg: int
    net: int  # page-local net id
    dx: float  # unit direction from the wire into the symbol
    dy: float
    comp: Optional[int] = None  # index into PageNetlist.components
    cluster: Optional[int] = None
    pin: Optional[str] = None
    pin_name: Optional[str] = None
    label: Optional[str] = None
    mark: Optional[tuple] = None  # drawn shape of the connector flag it ends on (comp is None)


@dataclass
class Component:
    page: int
    refdes: list[str]
    bbox: tuple[float, float, float, float]
    is_box: bool
    terminals: list[int] = field(default_factory=list)  # indices into PageNetlist.terminals
    # drawn body elements: "D" diagonal lines, "C" curves/circles, "T" closed triangles (diode,
    # LED, arrow heads), "B" boxes, "O" straight lines
    shape: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "/".join(self.refdes)

    @property
    def is_switch(self) -> bool:
        """Drawn like a switch/relay contact: a slanted blade between pin ends, not a box."""
        return not self.is_box and self.shape.get("D", 0) >= 1 and len(self.terminals) >= 2

    @property
    def is_coil(self) -> bool:
        """Drawn like an inductor/coil: arcs only, two pins."""
        return (
            not self.is_box
            and len(self.terminals) == 2
            and self.shape.get("C", 0) >= 1
            and self.shape.get("D", 0) == 0
            and self.shape.get("T", 0) == 0
        )

    @property
    def is_diode(self) -> bool:
        """Drawn like a diode / LED: a closed triangle between two pins, no box."""
        return not self.is_box and len(self.terminals) == 2 and self.shape.get("T", 0) >= 1

    @property
    def is_zigzag(self) -> bool:
        """Drawn like an ANSI resistor: a run of slanted strokes between two pins."""
        return not self.is_box and len(self.terminals) == 2 and self.shape.get("D", 0) >= 4 and not self.shape.get("C")


@dataclass
class PageNetlist:
    page: int  # 1-based
    width: float
    height: float
    segs: list[tuple[float, float, float, float]]
    seg_net: list[int]
    terminals: list[Terminal]
    components: list[Component]
    labels: list[tuple[str, int, Word]]  # (text, page-local net, word)
    words: list[Word]
    source: str = "vector"  # "vector" | "vector+ocr" | "raster"
    refdes_color: tuple = ()  # colour the refdes words were recognised by (empty: none available)
    scale: float = 1.0  # page text height / TEXT_UNIT (multiplies every geometric tolerance)
    text_size: Optional[float] = None  # measured legible text height (pt); None: no text layer


@dataclass
class Primitives:
    """Drawing primitives of one page, in PDF points, from vector data or a raster scan."""

    orth: list[tuple[float, float, float, float]]
    diag: list[tuple[float, float, float, float]]
    curves: list[tuple[float, float, float, float]]
    boxes: list[tuple[float, float, float, float]]
    dots: list[tuple[float, float, float]]  # (x, y, r)
    words: list[Word]
    tol: float = TOL
    tris: list[tuple[float, float, float, float]] = field(default_factory=list)  # closed triangles (bbox)
    colinear_tol: float = 0.05
    source: str = "vector"
    # scans lose junction dots: then a wire ending mid-way on a wire this long is a T-junction
    dotless_t_min: Optional[float] = None
    scale: float = 1.0  # page text height / TEXT_UNIT: scales every geometric tolerance
    text_size: Optional[float] = None
    strokes: list[tuple[float, float, float, float]] = field(default_factory=list)  # orthogonal lines of symbol graphics
    bars: list[tuple[float, float, float, float]] = field(default_factory=list)  # thick strokes drawn as slim rectangles
    rings: list[tuple[float, float, float, float]] = field(default_factory=list)  # closed round outlines (bbox)


# --------------------------------------------------------------------------- page extraction
def _color(obj: dict, key: str) -> tuple:
    c = obj.get(key)
    if isinstance(c, (list, tuple)):
        return tuple(round(float(v), 3) for v in c if isinstance(v, (int, float)))
    return ()


def _legible_floor(sizes: list[float]) -> float:
    """Font sizes under this are hidden fields / annotation (e.g. invisible 0.7 pt footprint text)."""
    s = sorted(sizes)
    return LEGIBLE_MIN * s[int(0.9 * (len(s) - 1))] if s else 0.0


def page_text_size(page, frac: float = TEXT_MIN) -> Optional[float]:
    """Height (pt) of the page's legible drawing text: median font size of the text of at least
    ``frac`` of the page's large text -- long runs of small annotation (footprint names) must not
    pull it down. None when the page has (almost) no text layer."""
    sizes = [c["size"] for c in page.chars if c.get("text", "").strip() and c.get("size", 0) > 0]
    if len(sizes) < 10:
        return None
    floor = _legible_floor(sizes) * frac / LEGIBLE_MIN
    legible = sorted(v for v in sizes if v >= floor)
    return legible[len(legible) // 2]


def document_scale(pdf_path: str, pages: Optional[list[int]] = None) -> float:
    """Fallback for pages without a text layer of their own: the document's legible text height
    / TEXT_UNIT (pdfium font sizes, fast). 1.0 when the document has too little text."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    hs: list[float] = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        for pno in pages or range(1, len(doc) + 1):
            tp = doc[pno - 1].get_textpage()
            n = tp.count_chars()
            for i in range(0, n, max(1, n // 200)):
                v = pdfium_c.FPDFText_GetFontSize(tp.raw, i)
                if 0 < v < 200:
                    hs.append(v)
    finally:
        doc.close()
    if len(hs) < 50:
        return 1.0
    floor = _legible_floor(hs)
    legible = sorted(v for v in hs if v >= floor)
    return legible[len(legible) // 2] / TEXT_UNIT


def _gather(page, dot_max: float = DOT_MAX, dot_fill_max: float = DOT_FILL_MAX, thin_max: float = THIN_MAX) -> tuple[list, ...]:
    orth: list[tuple[float, float, float, float]] = []
    diag: list[tuple[float, float, float, float]] = []
    curves: list[tuple[float, float, float, float]] = []
    boxes: list[tuple[float, float, float, float]] = []
    dots: list[tuple[float, float, float]] = []  # (x, y, r)
    tris: list[tuple[float, float, float, float]] = []
    eps = 0.05 * dot_max / DOT_MAX  # numerical noise of the drawing's coordinates, at this page's scale

    strokes: list[tuple[float, float, float, float]] = []
    bars: list[tuple[float, float, float, float]] = []
    rings: list[tuple[float, float, float, float]] = []

    def is_orth(a, b) -> bool:
        # a wire placed off-grid at one end comes out a hair slanted (well under 2 degrees); a
        # drawn stroke (zigzag, contact blade, chevron) is far steeper
        dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
        return min(dx, dy) <= max(eps, 0.03 * max(dx, dy))

    def add_seg(a, b, body_only: bool = False) -> None:
        (x0, y0), (x1, y1) = a, b
        dx, dy = abs(x0 - x1), abs(y0 - y1)
        if dx < eps and dy < eps:
            return
        if is_orth(a, b):
            if dx <= dy:
                x = (x0 + x1) / 2
                s = (x, min(y0, y1), x, max(y0, y1))
            else:
                y = (y0 + y1) / 2
                s = (min(x0, x1), y, max(x0, x1), y)
            (strokes if body_only else orth).append(s)
        else:
            diag.append((x0, y0, x1, y1))

    for o in page.lines:
        pts = o.get("pts") or []
        for a, b in zip(pts, pts[1:]):
            add_seg(a, b)

    for o in page.curves:
        x0, y0, x1, y1 = o["x0"], o["top"], o["x1"], o["bottom"]
        w, h = x1 - x0, y1 - y0
        path = o.get("path") or []
        has_curve = any(op[0] == "c" for op in path)
        pts = o.get("pts") or []
        distinct = len({(round(q[0], 2), round(q[1], 2)) for q in pts})
        if w <= dot_max and h <= dot_max and distinct == 3:
            tris.append((x0, y0, x1, y1))  # pin-direction arrowhead on a box edge: a mark, not a junction
            continue
        if w <= dot_max and h <= dot_max and (has_curve or o.get("fill")):
            dots.append(((x0 + x1) / 2, (y0 + y1) / 2, max(w, h) / 2))
            continue
        # a junction dot may be drawn larger, but it is always a filled round blob
        if has_curve and o.get("fill") and max(w, h) <= dot_fill_max and abs(w - h) <= 0.15 * max(w, h):
            dots.append(((x0 + x1) / 2, (y0 + y1) / 2, max(w, h) / 2))
            continue
        closed = bool(pts) and len(pts) > 2 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < eps
        if closed and not has_curve and distinct == 3:
            tris.append((x0, y0, x1, y1))  # diode / LED body, arrow head
            continue
        if has_curve or closed or not path:
            curves.append((x0, y0, x1, y1))
            if has_curve and closed and all(op[0] in "mch" for op in path) and min(w, h) > dot_fill_max:
                rings.append((x0, y0, x1, y1))  # a circle / ellipse outline (transistor, source, tube)
            continue
        # open polyline: split into segments. Wires are orthogonal, so a polyline with a slanted
        # stroke is symbol graphics as a whole (ground / power symbol, zigzag with its leads)
        ps, cur = [], None
        for op in path:
            if op[0] == "m":
                cur = op[1]
            elif op[0] == "l" and cur is not None:
                ps.append((cur, op[1]))
                cur = op[1]
        body = any(not is_orth(a, b) for a, b in ps if math.hypot(a[0] - b[0], a[1] - b[1]) >= eps)
        for a, b in ps:
            add_seg(a, b, body)

    pw, ph = float(page.width), float(page.height)
    borders = []
    for o in page.rects:
        x0, y0, x1, y1 = o["x0"], o["top"], o["x1"], o["bottom"]
        w, h = x1 - x0, y1 - y0
        dash = o.get("dash")
        if dash and dash[0]:
            continue  # dashed grouping frame, not a body
        if w > 0.6 * pw and h > 0.6 * ph:
            borders.append((x0, y0, x1, y1))
            continue  # page border
        if w <= dot_max and h <= dot_max:
            dots.append(((x0 + x1) / 2, (y0 + y1) / 2, max(w, h) / 2))
            continue
        if w < eps or h < eps:
            # degenerate rect = a line
            add_seg((x0, y0), (x1, y1))
            continue
        if min(w, h) <= thin_max and max(w, h) >= 4 * min(w, h):
            bars.append((x0, y0, x1, y1))  # a thick stroke drawn as a slim rectangle (capacitor plate)
            continue
        boxes.append((x0, y0, x1, y1))
    if borders:
        # the drawing frame: its zone ticks and the title block built into its corner are no circuit
        e = 0.2 * dot_max

        def on_border(x: float, y: float) -> bool:
            return any(
                (min(abs(x - b[0]), abs(x - b[2])) <= e and b[1] - e <= y <= b[3] + e)
                or (min(abs(y - b[1]), abs(y - b[3])) <= e and b[0] - e <= x <= b[2] + e)
                for b in borders
            )

        orth[:] = [s for s in orth if not (on_border(s[0], s[1]) or on_border(s[2], s[3]))]
        boxes[:] = [b for b in boxes if not any(on_border(x, y) for x in (b[0], b[2]) for y in (b[1], b[3]))]
    return orth, diag, curves, boxes, dots, tris, strokes, bars, rings


def _box_edge_dist(px: float, py: float, b: tuple) -> float:
    """Distance from a point to the outline of a box."""
    inside = b[0] <= px <= b[2] and b[1] <= py <= b[3]
    if inside:
        return min(px - b[0], b[2] - px, py - b[1], b[3] - py)
    return _pt_bbox_dist(px, py, b)


def _char_rotation(c: dict) -> str:
    """"up" (also upside-down), "ccw" (reads bottom-to-top) or "cw" (top-to-bottom)."""
    if c.get("upright", True):
        return "up"
    m = c.get("matrix") or (1, 0, 0, 1, 0, 0)
    if abs(m[1]) > abs(m[0]):
        return "ccw" if m[1] > 0 else "cw"
    return "up"


def page_words(page, min_size: float = 0.0, scale: float = 1.0) -> list[Word]:
    """Text of a page as words. Rotated text (pin numbers along vertical pins, side notes) is
    grouped separately with its own reading direction; otherwise its characters come out one by
    one. The text-flow grouping keeps one CAD label one word. Characters under ``min_size`` pt
    (hidden fields) are left out; ``scale`` sizes the letter-gap tolerance to the page's text."""
    if min_size > 0:
        page = page.filter(lambda o: o.get("object_type") != "char" or o.get("size", 0) >= min_size)
    # overprinted text (every glyph drawn twice) would otherwise read "RR1177" for R17
    seen: set = set()
    dup: set[int] = set()
    for c in page.chars:
        k = (c.get("text"), round(c["x0"], 2), round(c["top"], 2), round(c.get("size", 0), 2))
        if k in seen:
            dup.add(id(c))
        seen.add(k)
    if dup:
        page = page.filter(lambda o: id(o) not in dup)
    kinds = Counter(_char_rotation(c) for c in page.chars)
    opts = dict(x_tolerance=1.5 * scale, y_tolerance=1.5 * scale, extra_attrs=["non_stroking_color"])
    raw: list[dict] = []
    if kinds["ccw"] or kinds["cw"]:
        for rot, extra in (("up", {"use_text_flow": True}), ("ccw", {"char_dir_rotated": "btt"}), ("cw", {"char_dir_rotated": "ttb"})):
            if not kinds[rot]:
                continue
            sub = page.filter(lambda o, r=rot: o.get("object_type") != "char" or _char_rotation(o) == r)
            raw.extend(sub.extract_words(**opts, **extra))
    else:
        raw = page.extract_words(use_text_flow=True, **opts)
    return [
        Word(w["text"], w["x0"], w["top"], w["x1"], w["bottom"], _color(w, "non_stroking_color"))
        for w in raw
        if w["text"].strip()
    ]


def vector_primitives(page, scale: float = 1.0) -> Primitives:
    """``scale`` is only the fallback for a page without legible text of its own."""
    ts = page_text_size(page)
    if ts is not None:
        scale = ts / TEXT_UNIT
    # hidden fields: under LEGIBLE_MIN of the median of all text that is not tiny
    ts_all = page_text_size(page, LEGIBLE_MIN)
    floor = LEGIBLE_MIN * ts_all if ts_all is not None else 0.0
    # text flow: a CAD label is one text object; y-clustering chains rows ~1.5 pt apart (tiny labels
    # between normal ones) and then breaks the labels on that "line" into single characters
    words = page_words(page, floor * 0.99, scale)
    orth, diag, curves, boxes, dots, tris, strokes, bars, rings = _gather(page, DOT_MAX * scale, DOT_FILL_MAX * scale, THIN_MAX * scale)
    return Primitives(orth, diag, curves, boxes, dots, words, tol=TOL * scale, colinear_tol=0.05 * scale, scale=scale, tris=tris, text_size=ts, strokes=strokes, bars=bars, rings=rings)


def extract_page(page, pno: int, refdes_color_hint: tuple = (), scale: float = 1.0) -> PageNetlist:
    """Netlist of one page; pages without vector drawing go through the raster/OCR front-end."""
    from . import image_extract

    if image_extract.page_is_raster(page):
        prims = image_extract.raster_primitives(page)
    else:
        prims = vector_primitives(page, scale)
        if image_extract.text_is_outlined(page, prims.words):
            prims.words = image_extract.ocr_page_words(page)
            prims.source = "vector+ocr"
    return build_page(prims, pno, float(page.width), float(page.height), refdes_color_hint)


def _dedupe(items: list, key=lambda v: tuple(round(x, 2) for x in v)) -> list:
    """Overprinted graphics / text (drawn twice at the same place) count once."""
    seen: set = set()
    out = []
    for v in items:
        k = key(v)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _line_boxes(orth: list, tol: float, wmax: float, hmax: float, boxes: list) -> tuple[list, list]:
    """Rectangles drawn as four separate lines meeting only at their corners are boxes (up to
    wmax x hmax: a page-sized one is the drawing frame). One drawn inside a box is that symbol's
    own internal graphics and stays lines."""
    q = lambda v: round(v / tol)  # noqa: E731
    hs: dict[tuple, list[int]] = defaultdict(list)
    vs: dict[tuple, int] = {}
    ends: Counter = Counter()
    for i, (x0, y0, x1, y1) in enumerate(orth):
        ends[(q(x0), q(y0))] += 1
        ends[(q(x1), q(y1))] += 1
        if abs(y0 - y1) <= abs(x0 - x1):
            hs[(q(x0), q(x1))].append(i)
        else:
            vs[(q(x0), q(y0), q(y1))] = i
    used: set[int] = set()
    found = []
    for (a, b), idx in hs.items():
        idx = sorted(idx, key=lambda i: orth[i][1])
        for m, i in enumerate(idx):
            if i in used:
                continue
            for j in idx[m + 1:]:
                if j in used:
                    continue
                x0, y0, x1, _ = orth[i]
                y1 = orth[j][1]
                if x1 - x0 > wmax and y1 - y0 > hmax:
                    continue
                l, r = vs.get((a, q(y0), q(y1))), vs.get((b, q(y0), q(y1)))
                if l is None or r is None or l in used or r in used:
                    continue
                corners = [(a, q(y0)), (b, q(y0)), (a, q(y1)), (b, q(y1))]
                if all(ends[c] == 2 for c in corners) and not any(
                    (o[0] - tol <= x0 and x1 <= o[2] + tol and o[1] - tol <= y0 and y1 <= o[3] + tol)
                    or any(_box_edge_dist(x, y, o) <= tol for x in (x0, x1) for y in (y0, y1))
                    for o in boxes
                ):
                    used.update((i, j, l, r))
                    found.append((x0, y0, x1, y1))
                    break
    return [s for k, s in enumerate(orth) if k not in used], found


def build_page(prims: Primitives, pno: int, pw: float, ph: float, refdes_color_hint: tuple = ()) -> PageNetlist:
    orth, diag, curves, boxes, dots = (_dedupe(v) for v in (prims.orth, prims.diag, prims.curves, prims.boxes, prims.dots))
    orth, line_boxes = _line_boxes(orth, prims.tol, 0.5 * pw, 0.5 * ph, boxes)
    boxes += line_boxes
    words = _dedupe(prims.words, lambda w: (w.text, round(w.x0, 1), round(w.top, 1), round(w.x1, 1), round(w.bottom, 1)))
    prims.strokes, prims.tris = _dedupe(prims.strokes), _dedupe(prims.tris)
    tol, col_tol, t_min = prims.tol, prims.colinear_tol, prims.dotless_t_min
    S = prims.scale
    internal_max, tiny_net, pin_gap = INTERNAL_MAX * S, TINY_NET * S, PIN_GAP * S
    part_gap, join_gap, local_span = PART_GAP * S, JOIN_GAP * S, LOCAL_SPAN * S
    # small rects that merely frame a piece of text (boxed pin numbers, title cells) are not symbols
    if boxes:
        tg = Grid(10.0)
        for k, w in enumerate(words):
            tg.add(k, w.x0, w.top, w.x1, w.bottom)

        def is_frame(b: tuple) -> bool:
            if b[2] - b[0] > 40 * S or b[3] - b[1] > 14 * S:
                return False
            # a line ending on the outline from outside is a pin: a symbol body (resistor with its
            # designator printed inside), not a frame around a piece of text
            for x0, y0, x1, y1 in orth:
                for px, py, qx, qy in ((x0, y0, x1, y1), (x1, y1, x0, y0)):
                    if _box_edge_dist(px, py, b) <= tol and not (b[0] - tol <= qx <= b[2] + tol and b[1] - tol <= qy <= b[3] + tol):
                        return False
            area = (b[2] - b[0]) * (b[3] - b[1])
            for k in tg.query(*b):
                w = words[k]
                if b[0] - 0.5 * S <= w.x0 and w.x1 <= b[2] + 0.5 * S and b[1] - 0.5 * S <= w.top and w.bottom <= b[3] + 0.5 * S:
                    if (w.x1 - w.x0) * (w.bottom - w.top) >= 0.2 * area:
                        return True
            return False

        boxes = [b for b in boxes if not is_frame(b)]

    # graphics drawn inside a box symbol (a coil, diode or transistor within its outline, polarity
    # marks, internal switch diagrams) are body, not wires: drop them, and cut a pin line that runs
    # on into the box at the outline so the pin sits on the box edge. A grouping frame around a
    # sub-circuit keeps its content: it holds another box of symbol size, a junction dot or a
    # reference designator, and its wires never reach its outline -- symbol internals always do.
    refdes_color = _dominant_color([w for w in words if SUFFIXED_REFDES_RE.match(w.text)]) or refdes_color_hint
    if boxes and orth:
        nested_min = NESTED_BOX_MIN * S
        og = Grid(10.0)
        for i, (x0, y0, x1, y1) in enumerate(orth):
            og.add(i, x0, y0, x1, y1)
        rgrid = Grid(10.0)
        for k, w in enumerate(words):
            if _refdes_like(w, refdes_color):
                rgrid.add(k, w.cx, w.cy, w.cx, w.cy)
        dgrid0 = Grid(10.0)
        for k, (x, y, _r) in enumerate(dots):
            dgrid0.add(k, x, y, x, y)
        drop: set[int] = set()
        clip: dict[int, tuple[float, float, float, float]] = {}
        for b in boxes:
            if b[2] - b[0] > 0.5 * pw and b[3] - b[1] > 0.5 * ph:
                continue

            def deep(px: float, py: float) -> bool:
                return b[0] + tol < px < b[2] - tol and b[1] + tol < py < b[3] - tol

            def inside(px: float, py: float) -> bool:
                return b[0] - tol <= px <= b[2] + tol and b[1] - tol <= py <= b[3] + tol

            if any(
                o is not b and deep(o[0], o[1]) and deep(o[2], o[3]) and (o[2] - o[0] > nested_min or o[3] - o[1] > nested_min)
                for o in boxes
            ):
                continue
            if any(deep(dots[k][0], dots[k][1]) for k in dgrid0.query(*b)):
                continue
            if any(deep(words[k].cx, words[k].cy) for k in rgrid.query(*b)):
                continue
            inner: list[int] = []
            cuts: dict[int, tuple[float, float, float, float]] = {}
            reaches = False
            for i in og.query(b[0] - tol, b[1] - tol, b[2] + tol, b[3] + tol):
                x0, y0, x1, y1 = orth[i]
                in0, in1 = inside(x0, y0), inside(x1, y1)
                if in0 and in1:
                    inner.append(i)
                    if not (deep(x0, y0) and deep(x1, y1)):
                        reaches = True
                elif deep(x0, y0) and not in1:  # runs out of the box to the right / bottom
                    cuts[i] = (b[2], y0, x1, y1) if abs(y0 - y1) <= abs(x0 - x1) else (x0, b[3], x1, y1)
                    reaches = True
                elif deep(x1, y1) and not in0:  # comes in from the left / top
                    cuts[i] = (x0, y0, b[0], y1) if abs(y0 - y1) <= abs(x0 - x1) else (x0, y0, x1, b[1])
                    reaches = True
            if reaches:
                drop.update(inner)
                clip.update(cuts)
        if drop or clip:
            orth = [clip.get(i, s) for i, s in enumerate(orth) if i not in drop]

    # a line drawn within closed triangles (the anode-cathode line through a diode) is their body
    strokes = list(prims.strokes)
    # likewise everything drawn inside a round outline (transistor, source): lines wholly inside are
    # body, and a pin line that runs on into the outline ends at it
    rings = [b for b in _dedupe(prims.rings) if max(b[2] - b[0], b[3] - b[1]) <= 30 * S]
    if rings and orth:
        rgr = Grid(10.0)
        for k, b in enumerate(rings):
            rgr.add(k, *b)

        def ell(b: tuple, x: float, y: float, pad: float) -> float:
            rx, ry = (b[2] - b[0]) / 2 + pad, (b[3] - b[1]) / 2 + pad
            return ((x - (b[0] + b[2]) / 2) / rx) ** 2 + ((y - (b[1] + b[3]) / 2) / ry) ** 2

        keep = []
        for s in orth:
            for b in (rings[k] for k in rgr.query(*s)):
                in0, in1 = ell(b, s[0], s[1], tol) <= 1, ell(b, s[2], s[3], tol) <= 1
                if in0 and in1:
                    strokes.append(s)
                    break
                if in0 != in1 and ell(b, *((s[0], s[1]) if in0 else (s[2], s[3])), -tol) < 1:
                    cx, cy, rx, ry = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, (b[2] - b[0]) / 2, (b[3] - b[1]) / 2
                    if abs(s[1] - s[3]) <= abs(s[0] - s[2]):  # horizontal: cut where it crosses the outline
                        dx = rx * math.sqrt(max(0.0, 1 - ((s[1] - cy) / ry) ** 2))
                        s = (cx + dx, s[1], s[2], s[3]) if in0 else (s[0], s[1], cx - dx, s[3])
                    else:
                        dy = ry * math.sqrt(max(0.0, 1 - ((s[0] - cx) / rx) ** 2))
                        s = (s[0], cy + dy, s[2], s[3]) if in0 else (s[0], s[1], s[2], cy - dy)
                    if s[0] <= s[2] and s[1] <= s[3]:
                        keep.append(s)
                    break
            else:
                keep.append(s)
        orth = keep
    if prims.tris and orth:
        trg = Grid(10.0)
        for k, b in enumerate(prims.tris):
            trg.add(k, *b)

        def in_tri(x: float, y: float) -> bool:
            return any(b[0] - tol <= x <= b[2] + tol and b[1] - tol <= y <= b[3] + tol for b in (prims.tris[k] for k in trg.query(x, y, x, y)))

        keep = []
        for s in orth:
            within = in_tri(s[0], s[1]) and in_tri(s[2], s[3]) and in_tri((s[0] + s[2]) / 2, (s[1] + s[3]) / 2)
            (strokes if within else keep).append(s)
        orth = keep

    n = len(orth)
    uf = UnionFind()
    for i in range(n):
        uf.find(i)
    sgrid = Grid()
    for i, (x0, y0, x1, y1) in enumerate(orth):
        sgrid.add(i, x0 - tol, y0 - tol, x1 + tol, y1 + tol)

    def horiz(i: int) -> bool:
        return abs(orth[i][1] - orth[i][3]) <= abs(orth[i][0] - orth[i][2])

    # endpoint coincidence + colinear overlap
    endpoint_links = [[0, 0] for _ in range(n)]  # per seg, per end: number of connections
    for i, (x0, y0, x1, y1) in enumerate(orth):
        for e, (px, py) in enumerate(((x0, y0), (x1, y1))):
            for j in sgrid.query(px - tol, py - tol, px + tol, py + tol):
                if j == i:
                    continue
                a0, b0, a1, b1 = orth[j]
                if math.hypot(px - a0, py - b0) <= tol or math.hypot(px - a1, py - b1) <= tol:
                    uf.union(i, j)
                    endpoint_links[i][e] += 1
                elif horiz(i) == horiz(j) and _pt_seg_dist(px, py, a0, b0, a1, b1) <= col_tol:
                    uf.union(i, j)  # colinear overlap -> continuation
                    endpoint_links[i][e] += 1
                elif (
                    t_min is not None
                    and horiz(i) != horiz(j)
                    and max(a1 - a0, b1 - b0) >= t_min
                    and _pt_seg_dist(px, py, a0, b0, a1, b1) <= tol
                ):
                    uf.union(i, j)
                    endpoint_links[i][e] += 1

    # T-junctions at dots. A dot that only one line reaches joins nothing: it is symbol graphics
    # (the corner dots of a bridge rectifier), and the line ending in it stays a loose pin end
    dot_at: list[tuple[float, float, float]] = dots
    dgrid = Grid()
    for k, (x, y, r) in enumerate(dots):
        rr = max(r, tol) + 0.1
        hit = [j for j in sgrid.query(x - rr, y - rr, x + rr, y + rr) if _pt_seg_dist(x, y, *orth[j]) <= rr]
        if len(hit) < 2:
            continue
        dgrid.add(k, x - r, y - r, x + r, y + r)
        for j in hit[1:]:
            uf.union(hit[0], j)

    def at_dot(px: float, py: float) -> bool:
        for k in dgrid.query(px - tol, py - tol, px + tol, py + tol):
            x, y, r = dot_at[k]
            if math.hypot(px - x, py - y) <= r + tol:
                return True
        return False

    # body elements: ("D", seg) diag, ("C", bbox) curve, ("T", bbox) triangle, ("B", bbox) box, ("O", orth idx)
    elems: list[tuple[str, tuple]] = []
    for s in diag:
        elems.append(("D", s))
    for b in curves:
        elems.append(("C", b))
    for b in prims.tris:
        elems.append(("T", b))
    for b in boxes:
        elems.append(("B", b))
    for s in strokes:
        elems.append(("L", s))
    for b in _dedupe(prims.bars):
        elems.append(("P", b))

    def elem_bbox(e: tuple[str, tuple]) -> tuple:
        k, g = e
        if k in ("D", "L"):
            return (min(g[0], g[2]), min(g[1], g[3]), max(g[0], g[2]), max(g[1], g[3]))
        if k == "O":
            return orth[g]
        return g

    def touches(e: tuple[str, tuple], px: float, py: float) -> bool:
        k, g = e
        if k in ("D", "L"):
            return _pt_seg_dist(px, py, *g) <= tol
        if k in ("C", "T", "P"):
            return _pt_bbox_dist(px, py, g) <= tol
        if k == "B":
            return _box_edge_dist(px, py, g) <= tol
        return _pt_seg_dist(px, py, *orth[g]) <= tol

    egrid = Grid()
    for k, e in enumerate(elems):
        b = elem_bbox(e)
        egrid.add(k, b[0] - tol, b[1] - tol, b[2] + tol, b[3] + tol)

    # dangling endpoints
    dangling: list[tuple[int, int, float, float]] = []  # (seg, end, x, y)
    for i, (x0, y0, x1, y1) in enumerate(orth):
        for e, (px, py) in enumerate(((x0, y0), (x1, y1))):
            if endpoint_links[i][e] == 0 and not at_dot(px, py):
                dangling.append((i, e, px, py))

    # orth segments whose interior is touched (no dot) by a dangling end of ANOTHER net: body edges
    body_orth: set[int] = set()
    for i, _e, px, py in dangling:
        for j in sgrid.query(px - tol, py - tol, px + tol, py + tol):
            if j == i or uf.find(j) == uf.find(i) or horiz(i) == horiz(j):
                continue
            a0, b0, a1, b1 = orth[j]
            if _pt_seg_dist(px, py, a0, b0, a1, b1) <= tol and math.hypot(px - a0, py - b0) > tol and math.hypot(px - a1, py - b1) > tol:
                body_orth.add(j)

    # a short free-standing stroke that a wire end points at, across its path and within PIN_GAP, is
    # a symbol body (capacitor plate, battery cell): a thick stroke leaves half its width as a gap
    seg_count = Counter(uf.find(i) for i in range(n))
    plates: set[int] = set()
    for i, e, px, py in dangling:
        x0, y0, x1, y1 = orth[i]
        ox, oy = (x1, y1) if e == 0 else (x0, y0)
        L = math.hypot(px - ox, py - oy) or 1.0
        ux, uy = (px - ox) / L, (py - oy) / L
        for j in sgrid.query(px - pin_gap, py - pin_gap, px + pin_gap, py + pin_gap):
            if j == i or horiz(j) == horiz(i) or seg_count[uf.find(j)] != 1 or endpoint_links[j] != [0, 0]:
                continue
            a0, b0, a1, b1 = orth[j]
            if max(a1 - a0, b1 - b0) > internal_max:
                continue
            if horiz(j):
                t = (b0 - py) / uy if abs(uy) > 0.5 else -1.0
                hit = a0 - tol <= px <= a1 + tol
            else:
                t = (a0 - px) / ux if abs(ux) > 0.5 else -1.0
                hit = b0 - tol <= py <= b1 + tol
            if hit and 0 < t <= pin_gap:
                body_orth.add(j)
                plates.add(j)

    # preliminary clusters over non-orth elements
    cuf = UnionFind()

    def cluster_elems(elist: list[tuple[str, tuple]], grid: Grid) -> None:
        for k, e in enumerate(elist):
            cuf.find(k)
            b = elem_bbox(e)
            for m in grid.query(b[0] - tol, b[1] - tol, b[2] + tol, b[3] + tol):
                if m <= k:
                    continue
                e2 = elist[m]
                b2 = elem_bbox(e2)
                if _bbox_gap(b, b2) > tol:
                    continue
                # box vs something fully inside it (not touching its outline) is not a touch
                if e[0] == "B" or e2[0] == "B":
                    box, other = (b, b2) if e[0] == "B" else (b2, b)
                    if e[0] == "B" and e2[0] == "B":
                        continue  # two boxes are two symbols (e.g. stacked connector units)
                    if (other[0] > box[0] + tol and other[2] < box[2] - tol and other[1] > box[1] + tol and other[3] < box[3] - tol):
                        continue
                cuf.union(k, m)

    cluster_elems(elems, egrid)

    # "internal" orth nets (small line groups whose loose ends all sit on one symbol) -> body
    net_segs: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        net_segs[uf.find(i)].append(i)
    dangling_by_net: dict[int, list[tuple[int, int, float, float]]] = defaultdict(list)
    for d in dangling:
        dangling_by_net[uf.find(d[0])].append(d)

    def touched_elems(px: float, py: float) -> list[int]:
        return [k for k in egrid.query(px - tol, py - tol, px + tol, py + tol) if touches(elems[k], px, py)]

    for root, segs in net_segs.items():
        if len(segs) > 10:
            continue
        xs0 = min(orth[s][0] for s in segs)
        ys0 = min(orth[s][1] for s in segs)
        xs1 = max(orth[s][2] for s in segs)
        ys1 = max(orth[s][3] for s in segs)
        if xs1 - xs0 > internal_max or ys1 - ys0 > internal_max:
            continue
        ends = dangling_by_net.get(root, [])
        if not ends:
            continue
        roots = set()
        ok = True
        for _, _, px, py in ends:
            t = touched_elems(px, py)
            if not t:
                ok = False
                break
            roots.update(cuf.find(k) for k in t)
        tiny = xs1 - xs0 <= tiny_net and ys1 - ys0 <= tiny_net
        if ok and (len(roots) == 1 or tiny):
            body_orth.update(segs)

    # add body orth segments as elements and recluster
    for j in sorted(body_orth):
        elems.append(("O", j))
    egrid = Grid()
    for k, e in enumerate(elems):
        b = elem_bbox(e)
        egrid.add(k, b[0] - tol, b[1] - tol, b[2] + tol, b[3] + tol)
    cuf = UnionFind()
    cluster_elems(elems, egrid)
    body_orth_set = body_orth

    # terminals = dangling ends (of non-body nets) that touch a body element -- or that point at
    # one within PIN_GAP (symbols such as relay contacts leave a small gap after the pin line)
    terminals: list[Terminal] = []
    for i, e, px, py in dangling:
        if i in body_orth_set:
            continue
        x0, y0, x1, y1 = orth[i]
        ox, oy = (x1, y1) if e == 0 else (x0, y0)
        L = math.hypot(px - ox, py - oy) or 1.0
        ux, uy = (px - ox) / L, (py - oy) / L
        t = [k for k in touched_elems(px, py) if not (elems[k][0] == "O" and uf.find(elems[k][1]) == uf.find(i))]
        # a pin leaves its box outward; a line inside a box ending on its outline (a table rule of
        # the title block, a divider) is the box's own drawing
        def outward(k: int, ex: float, ey: float) -> bool:
            b = elems[k][1]
            if elems[k][0] != "B":
                return True
            if b[0] + tol < (px + ox) / 2 < b[2] - tol and b[1] + tol < (py + oy) / 2 < b[3] - tol:
                return False
            # a pin meets its box head-on; a line running along the outline does not enter it
            return min(abs(ex - b[0]), abs(ex - b[2])) <= tol if abs(uy) < 0.5 else min(abs(ey - b[1]), abs(ey - b[3])) <= tol

        t = [k for k in t if outward(k, px, py)]
        if not t:
            step = 0.25 * S
            while step <= pin_gap and not t:
                qx, qy = px + ux * step, py + uy * step
                t = [k for k in touched_elems(qx, qy) if (elems[k][0] != "O" or elems[k][1] in plates) and outward(k, qx, qy)]
                step += 0.25 * S
        if not t:
            continue
        terminals.append(Terminal(pno, px, py, i, uf.find(i), ux, uy, cluster=t[0]))

    # a symbol drawn as one stroke path (a power / ground symbol with its stem) may start right on
    # a wire end that also joins other wires (a junction): that contact point is its pin too
    tgrid0 = Grid(10.0)
    for ti, t in enumerate(terminals):
        tgrid0.add(ti, t.x, t.y, t.x, t.y)
    for k, (kind, s) in enumerate(elems):
        if kind != "L":
            continue
        for (px, py), (qx, qy) in (((s[0], s[1]), (s[2], s[3])), ((s[2], s[3]), (s[0], s[1]))):
            if any(math.hypot(terminals[ti].x - px, terminals[ti].y - py) <= tol for ti in tgrid0.query(px - tol, py - tol, px + tol, py + tol)):
                continue
            for j in sgrid.query(px - tol, py - tol, px + tol, py + tol):
                a0, b0, a1, b1 = orth[j]
                if j in body_orth or not (math.hypot(px - a0, py - b0) <= tol or math.hypot(px - a1, py - b1) <= tol):
                    continue
                L = math.hypot(qx - px, qy - py) or 1.0
                terminals.append(Terminal(pno, px, py, j, uf.find(j), (qx - px) / L, (qy - py) / L, cluster=k))
                tgrid0.add(len(terminals) - 1, px, py, px, py)
                break

    # small symbol pieces a hair apart (relay contact circles, capacitor plates) form one part
    root0 = [cuf.find(k) for k in range(len(elems))]
    pre_bbox: dict[int, list[float]] = {}
    pre_box: set[int] = set()
    for k, e in enumerate(elems):
        r, b = root0[k], elem_bbox(e)
        if r in pre_bbox:
            bb = pre_bbox[r]
            bb[0], bb[1], bb[2], bb[3] = min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])
        else:
            pre_bbox[r] = list(b)
        if e[0] == "B":
            pre_box.add(r)
    term_roots = {root0[t.cluster] for t in terminals}
    small = {r for r in term_roots if r not in pre_box and pre_bbox[r][2] - pre_bbox[r][0] <= 30 * S and pre_bbox[r][3] - pre_bbox[r][1] <= 30 * S}
    puf = UnionFind()
    for k, e in enumerate(elems):
        if root0[k] not in small:
            continue
        b = elem_bbox(e)
        for m in egrid.query(b[0] - part_gap, b[1] - part_gap, b[2] + part_gap, b[3] + part_gap):
            if root0[m] != root0[k] and root0[m] in small and _bbox_gap(b, elem_bbox(elems[m])) <= part_gap:
                puf.union(root0[k], root0[m])
    for k in range(len(elems)):
        cuf.union(puf.find(root0[k]), root0[k])
    # two parallel plates facing each other across a gap of about a text height, each fed by its
    # own wire from the outside, are one capacitor (or battery cell)
    plate_seg: dict[int, tuple] = {}  # elem -> centre line (x0, y0, x1, y1)
    feeds: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for t in terminals:
        feeds[t.cluster].append((t.dx, t.dy))
    for k, e in enumerate(elems):
        if k not in feeds:
            continue
        if e[0] == "O" and (e[1] in plates or (seg_count[uf.find(e[1])] == 1 and max(orth[e[1]][2] - orth[e[1]][0], orth[e[1]][3] - orth[e[1]][1]) <= internal_max)):
            plate_seg[k] = orth[e[1]]
        elif e[0] == "P":
            b = e[1]
            plate_seg[k] = (b[0], (b[1] + b[3]) / 2, b[2], (b[1] + b[3]) / 2) if b[2] - b[0] > b[3] - b[1] else ((b[0] + b[2]) / 2, b[1], (b[0] + b[2]) / 2, b[3])

    def faces(ka: int, kb: int) -> bool:
        """A wire feeds plate ka heading toward plate kb."""
        a, b = plate_seg[ka], plate_seg[kb]
        vx, vy = (b[0] + b[2] - a[0] - a[2]) / 2, (b[1] + b[3] - a[1] - a[3]) / 2
        return any(dx * vx + dy * vy > 0 for dx, dy in feeds[ka])

    pgrid = Grid(10.0)
    for k, s in plate_seg.items():
        pgrid.add(k, *s)
    reach = 8.0 * S
    for ka, s1 in plate_seg.items():
        hz = abs(s1[1] - s1[3]) <= abs(s1[0] - s1[2])
        for kb in pgrid.query(s1[0] - reach, s1[1] - reach, s1[2] + reach, s1[3] + reach):
            s2 = plate_seg[kb]
            if kb <= ka or hz != (abs(s2[1] - s2[3]) <= abs(s2[0] - s2[2])):
                continue
            lo, hi = (0, 2) if hz else (1, 3)
            ov = min(s1[hi], s2[hi]) - max(s1[lo], s2[lo])
            d = abs(s1[1] - s2[1]) if hz else abs(s1[0] - s2[0])
            if ov >= 0.5 * min(s1[hi] - s1[lo], s2[hi] - s2[lo]) and d <= reach and faces(ka, kb) and faces(kb, ka):
                cuf.union(ka, kb)
    for t in terminals:
        t.cluster = cuf.find(t.cluster)

    # cluster bboxes
    cl_bbox: dict[int, list[float]] = {}
    cl_isbox: dict[int, bool] = defaultdict(bool)
    for k, e in enumerate(elems):
        r = cuf.find(k)
        b = elem_bbox(e)
        if r in cl_bbox:
            bb = cl_bbox[r]
            bb[0], bb[1], bb[2], bb[3] = min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])
        else:
            cl_bbox[r] = list(b)
        if e[0] == "B":
            cl_isbox[r] = True
    frame = {r for r, bb in cl_bbox.items() if bb[2] - bb[0] > 0.5 * pw and bb[3] - bb[1] > 0.5 * ph}
    terminals = [t for t in terminals if t.cluster not in frame]
    term_clusters = {t.cluster for t in terminals}
    cl_elems: dict[int, list[tuple]] = defaultdict(list)
    for k, e in enumerate(elems):
        r = cuf.find(k)
        if r in term_clusters:
            cl_elems[r].append((e[0], elem_bbox(e)))

    # a small "x" of two crossing strokes at the end of a lone wire is a no-connect mark, not a
    # symbol (neighbouring marks may have merged into one cluster): the wire end stays a pin of
    # the part it comes from, the mark gets no terminal
    tcl: dict[int, list[Terminal]] = defaultdict(list)
    for t in terminals:
        tcl[t.cluster].append(t)

    def is_nc_mark(r: int) -> bool:
        el = cl_elems[r]
        tis = tcl.get(r, [])
        if not tis or len(el) != 2 * len(tis) or any(k != "D" for k, _ in el):
            return False
        for t in tis:
            near = [
                b for _, b in el
                if b[0] - tol <= t.x <= b[2] + tol and b[1] - tol <= t.y <= b[3] + tol
                and b[2] - b[0] <= 4 * S and b[3] - b[1] <= 4 * S
            ]
            if len(near) != 2:
                return False
        return True

    nc_marks = {r for r in cl_elems if is_nc_mark(r)}
    if nc_marks:
        terminals = [t for t in terminals if t.cluster not in nc_marks]
        term_clusters -= nc_marks
        for r in nc_marks:
            del cl_elems[r]

    def word_cluster_dist(wb: tuple, r: int) -> float:
        best = 1e9
        for kind, b in cl_elems[r]:
            if kind == "B" and b[0] <= wb[0] and wb[2] <= b[2] and b[1] <= wb[1] and wb[3] <= b[3]:
                d = min(wb[0] - b[0], b[2] - wb[2], wb[1] - b[1], b[3] - wb[3])
            else:
                d = _bbox_gap(wb, b)
            if d < best:
                best = d
        return best

    # ---------------------------------------------------------------- words
    # pin numbers: text right beside a terminal's pin line -- the straight run of wire that ends at
    # the pin, followed back through colinear pieces (exports split a stub at every text anchor)
    # up to PIN_RUN_MAX; the number nearest the symbol end wins
    pin_run_max = PIN_RUN_MAX * S

    def pin_run(t: Terminal) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = orth[t.seg]
        h = abs(t.dy) < 0.5
        # far end of the pin segment
        fx, fy = (x1, y1) if math.hypot(x0 - t.x, y0 - t.y) <= math.hypot(x1 - t.x, y1 - t.y) else (x0, y0)
        seen = {t.seg}
        while math.hypot(fx - t.x, fy - t.y) < pin_run_max and not at_dot(fx, fy):
            nxt = None
            for j in sgrid.query(fx - tol, fy - tol, fx + tol, fy + tol):
                if j in seen or horiz(j) != h:
                    continue
                a0, b0, a1, b1 = orth[j]
                if math.hypot(fx - a0, fy - b0) <= tol:
                    nxt, far = j, (a1, b1)
                elif math.hypot(fx - a1, fy - b1) <= tol:
                    nxt, far = j, (a0, b0)
                else:
                    continue
                break
            if nxt is None:
                break
            seen.add(nxt)
            fx, fy = far
        d = math.hypot(fx - t.x, fy - t.y)
        # a long pin drawn as one line of its own carries its number about midway
        own = math.hypot(x1 - x0, y1 - y0)
        reach = max(pin_run_max, min(own, 2.5 * pin_run_max)) if len(seen) == 1 else pin_run_max
        if d > reach:
            fx, fy = t.x - t.dx * reach, t.y - t.dy * reach
        return (min(t.x, fx), min(t.y, fy), max(t.x, fx), max(t.y, fy))

    wgrid = Grid(10.0)
    for k, w in enumerate(words):
        wgrid.add(k, w.x0, w.top, w.x1, w.bottom)

    heights = sorted(w.text_height for w in words)
    # titles / sheet headings are much taller than the drawing's text and are never net labels
    max_label_h = 2.2 * heights[len(heights) // 2] if heights else 1e9
    term_seg = {t.seg for t in terminals}
    term_net = {t.net for t in terminals}

    def on_wire(w: Word) -> Optional[int]:
        """The wire a text is written along: its baseline a hair off the line, the wire running
        under most of it (rotated text stands beside a vertical wire the same way), or level text
        starting right at a vertical wire. The wire connects something but is not itself a pin
        line (text over a pin line is the part's)."""
        h = w.text_height
        q = (w.x0 - 0.5 * h, w.top, w.x1 + 0.5 * h, w.bottom) if w.vertical else (w.x0 - 0.5 * h, w.bottom - 0.1 * h, w.x1 + 0.5 * h, w.bottom + 0.5 * h)
        for j in sgrid.query(*q):
            if j in body_orth_set or j in term_seg or uf.find(j) not in term_net:
                continue
            x0, y0, x1, y1 = orth[j]
            if horiz(j) == w.vertical:
                # level text that starts (or ends) right at a vertical wire, the wire passing its baseline
                ok = not w.vertical and (-0.2 * h <= w.x0 - x0 <= 0.4 * h or -0.2 * h <= x0 - w.x1 <= 0.4 * h) and y0 <= w.bottom <= y1 + 0.4 * h
            elif w.vertical:
                gap = min(abs(w.x0 - x0), abs(x0 - w.x1))
                ok = (w.x1 <= x0 or x0 <= w.x0) and gap <= 0.4 * h and min(y1, w.bottom) - max(y0, w.top) >= 0.6 * (w.bottom - w.top)
            else:
                ok = 0 <= y0 - w.bottom <= 0.4 * h and min(x1, w.x1) - max(x0, w.x0) >= 0.6 * (w.x1 - w.x0)
            if ok:
                return j
        return None

    def near_symbol(w: Word) -> bool:
        """Within 1.5 text heights of symbol graphics, but not inside a box (a pin name)."""
        m = 1.5 * w.text_height
        for k in egrid.query(w.x0 - m, w.top - m, w.x1 + m, w.bottom + m):
            b = elem_bbox(elems[k])
            if elems[k][0] == "B" and b[0] < w.cx < b[2] and b[1] < w.cy < b[3]:
                continue
            if _bbox_gap(w.bbox, b) < m:
                return True
        return False

    def in_phrase(w: Word) -> bool:
        return _in_phrase(words, wgrid, w)

    annotation: list[set[str]] = []

    def wire_under(w: Word) -> Optional[int]:
        """The wire a net name is written along (see on_wire). A designator or value stands by
        its symbol, and a text that annotates a symbol elsewhere on the page (a part number
        repeated by every relay) is no net name either."""
        if not re.search(r"[A-Za-z]", w.text) or VALUE_RE.match(w.text) or w.text_height > max_label_h:
            return None
        if near_symbol(w) or in_phrase(w):
            return None
        if not annotation:
            annotation.append({v.text for v in words if near_symbol(v) and on_wire(v) is None})
        return None if w.text in annotation[0] else on_wire(w)
    # when the drawing colours reference designators differently from plain pin numbers, a word
    # in the refdes colour is never a pin number (C7 beside a capacitor's pin is its refdes)
    pin_color = _dominant_color([w for w in words if re.fullmatch(r"\d{1,3}", w.text)])
    colour_split = bool(refdes_color and pin_color and pin_color != refdes_color)
    cand: list[tuple[float, int, int]] = []
    for ti, t in enumerate(terminals):
        x0, y0, x1, y1 = pin_run(t)
        for k in wgrid.query(x0 - 5 * S, y0 - 5 * S, x1 + 5 * S, y1 + 5 * S):
            w = words[k]
            if not PINNUM_RE.match(w.text) or (refdes_color and w.color == refdes_color and (len(w.text) > 2 or colour_split)):
                continue
            if re.match(r"[A-Z]+\d{3}", w.text):
                continue  # lettered pin numbers (grid arrays) run to two digits: U201A is a designator
            if VALUE_UNIT_RE.match(w.text) and not cl_isbox[t.cluster]:
                continue
            if not cl_isbox[t.cluster] and VALUE_RE.match(w.text) and not w.text.isdigit():
                continue  # a rating (10V, 2W) printed by a discrete part
            if abs(t.dy) < 0.5:  # horizontal pin line: number just above/below it
                if not (x0 - S <= w.cx <= x1 + S):
                    continue
                gap = y0 - w.bottom if w.cy < y0 else w.top - y0
                if gap < -1.2 * S or gap > 3.5 * S:
                    continue
                score = abs(gap) + 0.1 * abs(w.cx - t.x)
            else:  # vertical pin line: number left/right of it
                if not (y0 - S <= w.cy <= y1 + S):
                    continue
                gap = x0 - w.x1 if w.cx < x0 else w.x0 - x0
                if gap < -1.2 * S or gap > 3.5 * S:
                    continue
                score = abs(gap) + 0.1 * abs(w.cy - t.y)
            if not any(ch.isdigit() for ch in w.text):
                score += 3.0 * S  # a pin's name (G, K) may stand by it too: the number wins
            cand.append((score, ti, k))

    pin_word: dict[int, int] = {}  # terminal -> word taken as its pin number

    def assign_pins(prefer: set[int]) -> None:
        """Greedy nearest-first; terminals of the clusters in `prefer` are served first."""
        for ti in range(len(terminals)):
            terminals[ti].pin = None
        pin_word.clear()
        for w in words:
            if w.role == "pin":
                w.role = ""
        order = sorted(cand, key=lambda c: (0 if terminals[c[1]].cluster in prefer else 1, c))
        for _score, ti, k in order:
            if terminals[ti].pin is None and not words[k].role:
                terminals[ti].pin = words[k].text
                pin_word[ti] = k
                words[k].role = "pin"

    # first pass so that pin numbers that look like a refdes (C1, R2) are not taken for one
    assign_pins(set())

    def clear(x0: float, y0: float, x1: float, y1: float, own: int) -> bool:
        """True if no wire or other symbol graphic crosses the corridor box."""
        if x1 <= x0 or y1 <= y0:
            return True
        for j in sgrid.query(x0, y0, x1, y1):
            s = orth[j]
            if s[0] <= x1 and s[2] >= x0 and s[1] <= y1 and s[3] >= y0:
                return False
        for k in egrid.query(x0, y0, x1, y1):
            if cuf.find(k) == own:
                continue
            b = elem_bbox(elems[k])
            if b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0:
                return False
        return True

    def local_bb(r: int, t: Terminal) -> Optional[list[float]]:
        """Extent of the connector graphics next to this terminal (a wide cluster such as a
        column of stacked connectors must not lend its far edge to every terminal). Box
        outlines are never connector graphics."""
        horiz_t = abs(t.dy) < 0.5
        out: Optional[list[float]] = None
        for kind, b in cl_elems.get(r, []):
            if kind == "B":
                continue
            if horiz_t:
                if b[3] < t.y - 3 * S or b[1] > t.y + 3 * S:
                    continue
                if (t.dx < 0 and not (t.x - local_span <= b[2] and b[0] <= t.x + 0.5 * S)) or (
                    t.dx > 0 and not (t.x - 0.5 * S <= b[2] and b[0] <= t.x + local_span)
                ):
                    continue
            else:
                if b[2] < t.x - 3 * S or b[0] > t.x + 3 * S:
                    continue
                if (t.dy < 0 and not (t.y - local_span <= b[3] and b[1] <= t.y + 0.5 * S)) or (
                    t.dy > 0 and not (t.y - 0.5 * S <= b[3] and b[1] <= t.y + local_span)
                ):
                    continue
            out = list(b) if out is None else [min(out[0], b[0]), min(out[1], b[1]), max(out[2], b[2]), max(out[3], b[3])]
        return out

    # refdes -> symbol cluster: each refdes goes to its nearest symbol, preferring symbols it
    # sits above (the usual placement), so stacked units (J891A over J891B) stay apart
    cl_refdes: dict[int, list[str]] = defaultdict(list)
    pairs: list[tuple[float, int, int]] = []
    # without a refdes colour, text repeated on the page is a part value (AGN2004H), not a refdes
    # (a mention inside a line of notes -- "K1 L1 L2 0.98" -- is no repeat of the part's label)
    repeats = Counter(w.text for w in words if not _in_phrase(words, wgrid, w)) if not refdes_color else Counter()
    # text inside a box symbol level with one of its pins, starting right at that pin, is the
    # pin's name (LED2, A0) even when it reads like a designator
    in_box_name: set[int] = set()
    nterm0 = Counter(t.cluster for t in terminals)
    for t in terminals:
        if not cl_isbox[t.cluster] or nterm0[t.cluster] < 3:
            continue
        for k in wgrid.query(t.x - 12 * S, t.y - 12 * S, t.x + 12 * S, t.y + 12 * S):
            w = words[k]
            h = w.text_height
            if not any(kind == "B" and b[0] < w.cx < b[2] and b[1] < w.cy < b[3] for kind, b in cl_elems[t.cluster]):
                continue
            if abs(t.dy) < 0.5:
                if abs(w.cy - t.y) <= 0.6 * h and min(abs(w.x0 - t.x), abs(w.x1 - t.x)) <= 3 * h:
                    in_box_name.add(k)
            elif abs(w.cx - t.x) <= 0.6 * h and min(abs(w.top - t.y), abs(w.bottom - t.y)) <= 3 * h:
                in_box_name.add(k)

    # a one-pin symbol with a name written on its far side is a connector flag: a designator-looking
    # word further away than that name (a neighbour's value) is not its refdes
    flag_name_d: dict[int, float] = {}
    for ti, t in enumerate(terminals):
        r = t.cluster
        if nterm0[r] != 1 or cl_isbox[r]:
            continue
        lbb = local_bb(r, t)
        if lbb is not None:
            hit = _far_word(words, wgrid, t, lbb, lambda a, b_, c, d_, r=r: clear(a, b_, c, d_, r), S, max_label_h)
            if hit is not None and not _refdes_like(words[hit[0]], refdes_color):
                flag_name_d[r] = hit[1]

    prefix_n = Counter(re.match(r"[A-Z]*", w.text).group() for w in words if REFDES_RE.match(w.text))
    for k, w in enumerate(words):
        if w.role or repeats[w.text] > 2 or not _refdes_like(w, refdes_color) or k in in_box_name:
            continue
        if refdes_color and w.color and w.color != refdes_color:
            continue
        # a part number (LM317, VCA810) reads like a designator too, but its prefix is rare on the
        # page and its number long: it only names a symbol no real designator claims
        m = re.match(r"([A-Z]*)(\d*)", w.text)
        odd = 10.0 * S if prefix_n[m.group(1)] <= 1 and len(m.group(2)) >= 3 else 0.0
        for r in term_clusters:
            bb = cl_bbox[r]
            # how far a symbol's designator may stand: a flag none, a two-pin part some text
            # heights, a large part (op-amp, IC) further still
            lim = (15.0 if nterm0[r] < 2 else 25.0 if nterm0[r] == 2 else REFDES_REACH) * S
            if _bbox_gap(w.bbox, tuple(bb)) >= lim:
                continue
            d = word_cluster_dist(w.bbox, r)
            if r in flag_name_d and d > flag_name_d[r]:
                continue
            if cl_isbox[r] and nterm0[r] >= 3 and bb[0] < w.cx < bb[2] and bb[1] < w.cy < bb[3]:
                d = min(d, 2.0 * S)  # a designator printed inside a large part's body (not a pin name)
            if d < lim:
                if w.top > bb[1] + S:
                    d += 1.5 * S  # not above the symbol
                if not (bb[0] - S <= w.cx <= bb[2] + S) and w.bottom > bb[1] and w.top < bb[3]:
                    d += 2.0 * S  # beside the symbol, not centred over / under it: a neighbour's label passing by
                pairs.append((d + odd, k, r))
    pairs.sort()
    # every symbol carries its own designator: words and symbols are matched one to one at the
    # least total distance (C4 printed against C3's plate still goes to the capacitor below it);
    # a designator left over (a second unit's name) joins its nearest symbol. Units of one part
    # (K1A, K1B) nearest to the same symbol drawing stay together on it
    nearest: dict[int, tuple[float, int]] = {}
    for d, k, r in pairs:
        nearest.setdefault(k, (d, r))
    units: dict[tuple, list[int]] = defaultdict(list)
    for k, (_d, r) in nearest.items():
        units[(re.sub(r"[A-Z]$", "", words[k].text), r)].append(k)
    fixed = {k: nearest[k] for ks in units.values() if len(ks) > 1 for k in ks}
    taken = {r for _d, r in fixed.values()}
    best_of = _match([p for p in pairs if p[1] not in fixed and p[2] not in taken], REFDES_REACH * S)
    best_of.update(fixed)
    for d, k, r in pairs:
        if k not in best_of and d < 15.0 * S and wire_under(words[k]) is None:  # (one written along a wire is a net name)
            best_of[k] = (d, r)
    for k, (_d, r) in sorted(best_of.items(), key=lambda kv: kv[1][0]):
        cl_refdes[r].append(words[k].text)
        words[k].role = "refdes"
    # a part number printed by the symbol (AD8051, BC337) reads like a designator too; of two such
    # words on one symbol the designator is the one whose prefix the page uses more often, with
    # the shorter number
    prefix_n = Counter(re.match(r"[A-Z]*", w.text).group() for w in words if REFDES_RE.match(w.text))
    text_n = Counter(w.text for w in words)

    def ref_rank(t: str) -> tuple:
        # (prefix rarity, repeated on the page -- a value repeats, a designator does not -- digits)
        m = re.match(r"([A-Z]*)(\d*)", t)
        return (-prefix_n[m.group(1)], text_n[t] > 1, len(m.group(2)))

    for r, refs in cl_refdes.items():
        if len(refs) > 1:
            best = min(ref_rank(t) for t in refs)
            drop = {t for t in refs if ref_rank(t)[:2] > best[:2] or (ref_rank(t)[:2] == best[:2] and ref_rank(t)[2] > best[2] and ref_rank(t)[2] >= 3)}
            if drop:
                cl_refdes[r] = [t for t in refs if t not in drop]
                for k in best_of:
                    if words[k].text in drop and best_of[k][1] == r:
                        words[k].role = ""
    # a small symbol is never anonymous while a designator-like word sits by one of its pins: when
    # that is the only number on its pins ("C1" beside one capacitor lead, the other lead bare) it
    # is the symbol's refdes, not a pin number
    pin_words: dict[int, list[int]] = defaultdict(list)
    for ti, k in pin_word.items():
        pin_words[terminals[ti].cluster].append(k)
    # letter prefixes that number several pins of one symbol (grid arrays: A1, B17) are pin numbering
    pin_prefixes = {
        p for ks in pin_words.values()
        for p, n in Counter(re.match(r"[A-Z]*", words[k].text).group() for k in ks).items() if p and n > 1
    }
    nterm = Counter(t.cluster for t in terminals)
    for r in term_clusters:
        ks = pin_words.get(r, [])
        if r in cl_refdes or not 2 <= nterm[r] <= 3 or len(ks) != 1:
            continue
        m = re.fullmatch(r"([A-Z]{1,3})\d{1,4}", words[ks[0]].text)
        if m and m.group(1) not in pin_prefixes:
            cl_refdes[r].append(words[ks[0]].text)
            words[ks[0]].role = "refdes"

    # hierarchical block / sheet symbols: a box with several pins and no reference designator is
    # a part too when it carries a title at its top-left corner or when its pins are numbered
    by_cluster: dict[int, list[int]] = defaultdict(list)
    for ti, t in enumerate(terminals):
        by_cluster[t.cluster].append(ti)
    nblock = 0
    for r, tis in by_cluster.items():
        if r in cl_refdes or not cl_isbox[r] or len(tis) < 3:
            continue
        numbered = sum(1 for ti in tis if terminals[ti].pin) >= max(2, len(tis) // 2)
        b = cl_bbox[r]
        best, bd = None, 1e9
        for k in wgrid.query(b[0] - 2 * S, b[1] - 10 * S, b[0] + 0.25 * (b[2] - b[0]), b[1] + 1):
            w = words[k]
            if w.role or w.vertical or w.bottom > b[1] + 1 or w.x0 < b[0] - 2 * S:
                continue
            if not (REFDES_RE.match(w.text) or REFDES_LONG_RE.match(w.text) or REFDES_LOOSE_RE.match(w.text)):
                continue  # a heading above the box ("CHANNEL") is not its name
            d = (b[1] - w.bottom) + abs(w.x0 - b[0])
            if d < bd:
                best, bd = k, d
        if best is None and numbered and refdes_color:
            # or a name in the refdes colour beside the box (rotated along a tall box, for instance)
            for k in wgrid.query(b[0] - 15 * S, b[1] - 15 * S, b[2] + 15 * S, b[3] + 15 * S):
                w = words[k]
                if w.role or w.color != refdes_color or not _labelish(w.text) or len(w.text) < 2:
                    continue
                if b[0] <= w.cx <= b[2] and b[1] <= w.cy <= b[3]:
                    continue  # inside: a pin name
                d = _bbox_gap(w.bbox, tuple(b))
                if d < 15 * S and d < bd:
                    best, bd = k, d
        if best is None and not numbered:
            continue
        nblock += 1
        if best is not None:
            cl_refdes[r].append(words[best].text)
            words[best].role = "refdes"
        else:
            cl_refdes[r].append(f"BLOCK{pno}_{nblock}")

    # anonymous clusters: off-page/power connectors if a label sits on their far side,
    # otherwise part of the nearest refdes'd cluster (e.g. a relay's separate contact circle)
    terms_by_cl: dict[int, list[int]] = defaultdict(list)
    for ti, t in enumerate(terminals):
        terms_by_cl[t.cluster].append(ti)

    labels: list[tuple[str, int, Word]] = []
    # pieces right next to a refdes'd symbol (a lone relay contact circle) belong to it
    for r, tis in terms_by_cl.items():
        if r in cl_refdes:
            continue
        bb = tuple(cl_bbox[r])
        best, bd = None, join_gap
        for r2 in cl_refdes:
            b2 = tuple(cl_bbox[r2])
            if cl_isbox[r2] and _bbox_gap(bb, b2) > 0:
                continue
            d = _bbox_gap(bb, b2)
            if d <= bd:
                best, bd = r2, d
        if best is not None:
            cl_elems[best].extend(cl_elems.get(r, []))
            for ti in tis:
                terminals[ti].cluster = best
    # second pass: a pin number belongs to the part's pin, never to the connector flag that
    # shares the same stub of wire (flags carry net names, not pin numbers)
    assign_pins(set(cl_refdes))
    terms_by_cl = defaultdict(list)
    for ti, t in enumerate(terminals):
        terms_by_cl[t.cluster].append(ti)
    # a label goes to the flag it sits closest to: a flag whose own text is missing may otherwise
    # reach across (up to FAR_LABEL_MAX) and take the label of a neighbouring connector
    own = FAR_LABEL_OWN * S
    flag_terms = [ti for r, tis in terms_by_cl.items() if r not in cl_refdes for ti in tis]
    tgrid = Grid(10.0)
    for ti in flag_terms:
        tgrid.add(ti, terminals[ti].x, terminals[ti].y, terminals[ti].x, terminals[ti].y)

    def owned_elsewhere(k: int, ti: int, gap: float) -> bool:
        """Word k is within FAR_LABEL_OWN of another flag's wire end while terminal ti is far away."""
        if gap <= own:
            return False
        w = words[k]
        for tj in tgrid.query(w.x0 - own, w.top - own, w.x1 + own, w.bottom + own):
            if tj == ti:
                continue
            u = terminals[tj]
            if abs(u.y - w.cy) <= 2.6 * S and min(abs(u.x - w.x0), abs(u.x - w.x1)) <= own:
                return True
        return False

    claims: dict[int, tuple[float, int, int]] = {}  # word -> (score, cluster, terminal)
    pending = deque((r, ti) for r, tis in terms_by_cl.items() if r not in cl_refdes for ti in tis)
    while pending:
        r, ti = pending.popleft()
        t = terminals[ti]
        lbb = local_bb(r, t)
        if lbb is None:
            continue
        hit = _far_word(
            words, wgrid, t, lbb, lambda a, b_, c, d_, r=r: clear(a, b_, c, d_, r), S, max_label_h,
            blocked=lambda k, d, ti=ti: (k in claims and claims[k][0] <= d) or owned_elsewhere(k, ti, d),
        )
        if hit is None:
            continue
        k, d = hit
        if k in claims:
            pending.append(claims[k][1:])  # the flag that loses the word searches again
        claims[k] = (d, r, ti)
    got: set[int] = set()
    for k, (_d, r, ti) in claims.items():
        w, t = words[k], terminals[ti]
        w.role = "label"
        t.label = w.text
        labels.append((w.text, t.net, w))
        got.add(r)
    for r, tis in terms_by_cl.items():
        if r in cl_refdes or r in got:
            continue
        bb = cl_bbox[r]
        best, bd = None, 4.0 * S
        for r2 in cl_refdes:
            d = _bbox_gap(tuple(bb), tuple(cl_bbox[r2]))
            if d < bd:
                best, bd = r2, d
        if best is not None:
            cl_elems[best].extend(cl_elems.get(r, []))
            for ti in tis:
                terminals[ti].cluster = best

    # components
    comps: list[Component] = []
    cl_comp: dict[int, int] = {}
    for r, refs in cl_refdes.items():
        cl_comp[r] = len(comps)
        shape = dict(Counter("O" if kind in ("L", "P") else kind for kind, _ in cl_elems.get(r, [])))
        comps.append(Component(pno, sorted(set(refs)), tuple(cl_bbox[r]), cl_isbox[r], shape=shape))
    for ti, t in enumerate(terminals):
        ci = cl_comp.get(t.cluster)
        if ci is not None:
            t.comp = ci
            comps[ci].terminals.append(ti)

    # the drawn shape of each connector flag, in the frame of its wire (power and ground symbols
    # are identical copies, so the same shape stands for the same net -- see build_design)
    g = 0.5 * S
    for t in terminals:
        if t.comp is not None:
            continue
        el = cl_elems.get(t.cluster, [])
        bb = cl_bbox.get(t.cluster)
        if not el or (len(el) < 2 and el[0][0] in "OLPD") or bb is None or max(bb[2] - bb[0], bb[3] - bb[1]) > 30 * S or any(kind == "B" for kind, _ in el):
            continue  # (a lone stroke is a piece of some symbol, not a connector flag)
        sig = []
        for kind, b in el:
            xs, ys = [], []
            for x, y in ((b[0], b[1]), (b[2], b[3]), (b[0], b[3]), (b[2], b[1])):
                vx, vy = x - t.x, y - t.y
                xs.append(round((-vx * t.dy + vy * t.dx) / g))
                ys.append(round((vx * t.dx + vy * t.dy) / g))
            sig.append(("O" if kind in ("L", "P") else kind, min(xs), min(ys), max(xs), max(ys)))
        t.mark = tuple(sorted(sig))

    # pin names: text inside a box symbol, level with the pin
    for c in comps:
        if not c.is_box:
            continue
        b = c.bbox
        for ti in c.terminals:
            t = terminals[ti]
            best, bd = None, 60.0 * S
            for k in wgrid.query(b[0], b[1], b[2], b[3]):
                w = words[k]
                if w.role or not (b[0] - 0.5 * S <= w.cx <= b[2] + 0.5 * S and b[1] - 0.5 * S <= w.cy <= b[3] + 0.5 * S):
                    continue
                if abs(t.dy) < 0.5:
                    if abs(w.cy - t.y) > 2.6 * S:
                        continue
                    d = min(abs(w.x0 - t.x), abs(w.x1 - t.x))
                else:
                    if abs(w.cx - t.x) > 2.6 * S:
                        continue
                    d = min(abs(w.top - t.y), abs(w.bottom - t.y))
                if d < bd:
                    best, bd = w, d
            if best is not None:
                t.pin_name = best.text
        names = {terminals[ti].pin_name for ti in c.terminals if terminals[ti].pin_name}
        if names:
            for k in wgrid.query(b[0], b[1], b[2], b[3]):
                w = words[k]
                if w.text in names and not w.role and b[0] <= w.cx <= b[2] and b[1] <= w.cy <= b[3]:
                    w.role = "pinname"

    # net aliases: leftover text (in the connector-label colour) sitting right on top of a wire
    label_color = _dominant_color([w for _, _, w in labels])
    for w in words:
        if w.role or not label_color or w.color != label_color or len(w.text) < 3:
            continue
        if REFDES_RE.match(w.text) or PINNUM_RE.match(w.text):
            continue
        for j in sgrid.query(w.x0, w.bottom - 1, w.x1, w.bottom + 2.5 * S):
            if j in body_orth_set or not horiz(j):
                continue
            x0, y0, x1, _ = orth[j]
            if 0 <= y0 - w.bottom <= 2.0 * S and x0 - 0.5 * S <= w.x0 and w.x1 <= x1 + 0.5 * S:
                w.role = "label"
                labels.append((w.text, uf.find(j), w))
                break

    # local net labels: a name written along a wire (see wire_under)
    for w in words:
        if w.role:
            continue
        j = wire_under(w)
        if j is not None:
            w.role = "label"
            labels.append((w.text, uf.find(j), w))

    seg_net = [uf.find(i) for i in range(n)]
    return PageNetlist(pno, pw, ph, orth, seg_net, terminals, comps, labels, words, prims.source, refdes_color, S, prims.text_size)


def _match(pairs: list[tuple[float, int, int]], miss: float) -> dict[int, tuple[float, int]]:
    """Least-cost one-to-one assignment of words k to clusters r over candidate (cost, k, r)
    pairs; a word may stay unassigned at cost ``miss``. Exact (over its 4 nearest) per connected group of up to 8
    words, greedy nearest-first beyond."""
    by_k: dict[int, list[tuple[float, int]]] = defaultdict(list)
    by_r: dict[int, list[int]] = defaultdict(list)
    for d, k, r in pairs:
        by_k[k].append((d, r))
        by_r[r].append(k)
    out: dict[int, tuple[float, int]] = {}
    seen: set[int] = set()
    for k0 in by_k:
        if k0 in seen:
            continue
        group, todo = [], [k0]
        seen.add(k0)
        while todo:
            k = todo.pop()
            group.append(k)
            for _d, r in by_k[k]:
                for k2 in by_r[r]:
                    if k2 not in seen:
                        seen.add(k2)
                        todo.append(k2)
        if len(group) > 8:
            used: set[int] = set()
            for d, k, r in sorted((d, k, r) for k in group for d, r in by_k[k]):
                if k not in out and r not in used:
                    out[k] = (d, r)
                    used.add(r)
            continue
        group.sort(key=lambda k: min(d for d, _ in by_k[k]))
        best: list = [math.inf, {}]

        def dfs(i: int, cost: float, used: set, cur: dict) -> None:
            if cost >= best[0]:
                return
            if i == len(group):
                best[0], best[1] = cost, dict(cur)
                return
            k = group[i]
            for d, r in sorted(by_k[k])[:4]:
                if r not in used:
                    used.add(r)
                    cur[k] = (d, r)
                    dfs(i + 1, cost + d, used, cur)
                    del cur[k]
                    used.discard(r)
            dfs(i + 1, cost + miss, used, cur)

        dfs(0, 0.0, set(), {})
        out.update(best[1])
    return out


def _dominant_color(words: list[Word]) -> tuple:
    counts: dict[tuple, int] = defaultdict(int)
    for w in words:
        counts[w.color] += 1
    if not counts:
        return ()
    c, k = max(counts.items(), key=lambda kv: kv[1])
    return c if k >= 3 else ()


def _in_phrase(words: list[Word], wgrid: Grid, w: Word) -> bool:
    """Another word on the same line a space away: part of a note or value, never one net name."""
    h = w.text_height
    if w.vertical:
        return any(v is not w and v.vertical and abs(v.cx - w.cx) < 0.3 * h and v.bottom > w.top - h and v.top < w.bottom + h
                   for v in (words[k] for k in wgrid.query(w.x0, w.top - h, w.x1, w.bottom + h)))
    return any(v is not w and not v.vertical and abs(v.cy - w.cy) < 0.3 * h and v.x1 > w.x0 - h and v.x0 < w.x1 + h
               for v in (words[k] for k in wgrid.query(w.x0 - h, w.top, w.x1 + h, w.bottom)))


def _labelish(text: str) -> bool:
    # a two-letter word (HT, OE) can be a name; single letters are zone marks and pin names
    return bool(re.search(r"[A-Za-z0-9]", text)) and not VALUE_RE.match(text) and (not PINNUM_RE.match(text) or bool(re.fullmatch(r"[A-Z]{2}", text)))


def _far_word(
    words: list[Word],
    wgrid: Grid,
    t: Terminal,
    bb: list[float],
    clear=None,
    S: float = 1.0,
    max_h: float = 1e9,
    blocked=None,
) -> Optional[tuple[int, float]]:
    """(word index, score) of the text on the far side of a connector symbol, level with the
    terminal. Text further than FAR_LABEL_NEAR away only counts with a clear line of sight to the
    symbol. The score is the gap plus the row offset, so with pin rows closer than one text
    height the word level with this terminal wins; ``blocked(k, score)`` tells whether word k is
    already held by a terminal that scores better."""
    far_max, far_near = FAR_LABEL_MAX * S, FAR_LABEL_NEAR * S
    best, bd = None, far_max
    if abs(t.dy) < 0.5:
        if t.dx < 0:  # symbol extends left of the wire -> label further left
            q = (bb[0] - far_max, t.y - 4 * S, bb[0] + 1, t.y + 4 * S)
        else:
            q = (bb[2] - 1, t.y - 4 * S, bb[2] + far_max, t.y + 4 * S)
        for k in wgrid.query(*q):
            w = words[k]
            # level with the pin: within a fixed band, or within its own text height for big text;
            # rotated text stands across the pin's level
            if w.vertical:
                level = w.top <= t.y <= w.bottom
            else:
                level = abs(w.cy - t.y) <= max(2.6 * S, 0.6 * w.text_height)
            if w.role or not level or not _labelish(w.text) or w.text_height > max_h:
                continue
            if PINNUM_RE.match(w.text) and _in_phrase(words, wgrid, w):
                continue  # a two-letter word inside a note (R5 TO GND) is no name
            d = bb[0] - w.x1 if t.dx < 0 else w.x0 - bb[2]
            score = d + (0.0 if w.vertical else abs(w.cy - t.y))
            # a label may run into its flag's graphics by a fraction of its own text height
            if not (-max(S, 0.75 * w.text_height) <= d and score < bd) or (blocked is not None and blocked(k, score)):
                continue
            if d > far_near and clear is not None:
                corridor = (w.x1 + 0.3 * S, t.y - 0.8 * S, bb[0] - 0.3 * S, t.y + 0.8 * S) if t.dx < 0 else (bb[2] + 0.3 * S, t.y - 0.8 * S, w.x0 - 0.3 * S, t.y + 0.8 * S)
                if not clear(*corridor):
                    continue
            best, bd = k, score
    else:
        if t.dy < 0:  # symbol extends upward -> label above
            q = (t.x - 30 * S, bb[1] - 15 * S, t.x + 30 * S, bb[1] + 1)
        else:
            q = (t.x - 30 * S, bb[3] - 1, t.x + 30 * S, bb[3] + 15 * S)
        for k in wgrid.query(*q):
            w = words[k]
            # a rotated label stands on the pin line; a horizontal one is centred on it. Sheet
            # titles are never rotated, so the height cut-off applies to horizontal text only
            if w.role or not (w.x0 - max(2 * S, w.text_height) <= t.x <= w.x1 + max(2 * S, w.text_height)) or not _labelish(w.text) or (not w.vertical and w.text_height > max_h):
                continue
            if PINNUM_RE.match(w.text) and _in_phrase(words, wgrid, w):
                continue  # a two-letter word inside a note (R5 TO GND) is no name
            d = bb[1] - w.bottom if t.dy < 0 else w.top - bb[3]
            # a rotated label may run into the flag by up to its own text height (a trailing "_")
            if not (-(w.text_height if w.vertical else 0.8 * w.text_height) <= d < 12.0 * S):
                continue
            # several words may sit under a ground stem (its name and a neighbour's part value):
            # the one centred on the pin is the label
            score = d + 0.5 * abs(w.cx - t.x)
            if score < bd and not (blocked is not None and blocked(k, score)):
                best, bd = k, score
    return None if best is None else (best, bd)


# --------------------------------------------------------------------------- design
@dataclass
class Design:
    pages: list[PageNetlist]
    net_uf: UnionFind
    # global net id -> data
    net_labels: dict[tuple, set[str]]
    label_nets: dict[str, tuple]
    net_terms: dict[tuple, list[tuple[int, int]]]  # gnet -> [(page_idx, terminal_idx)]
    scale: float = 1.0  # drawing scale relative to the reference export (document_scale)

    def gnet(self, page_idx: int, local_net: int) -> tuple:
        return self.net_uf.find((page_idx, local_net))

    def term(self, ref: tuple[int, int]) -> Terminal:
        return self.pages[ref[0]].terminals[ref[1]]

    def comps(self) -> Iterable[tuple[int, int, Component]]:
        for pi, pn in enumerate(self.pages):
            for ci, c in enumerate(pn.components):
                yield pi, ci, c

    def comp_terms(self, pi: int, ci: int) -> list[tuple[int, int]]:
        return [(pi, ti) for ti in self.pages[pi].components[ci].terminals]


def build_design(pages: list[PageNetlist]) -> Design:
    # a text found on two nets of one page that carry two other, different names -- names that
    # nothing else joins -- is no net name (a flag that marks a net, not one naming it): it must
    # not join them, here or across pages
    on_net: dict[tuple, set[str]] = defaultdict(set)
    for pi, pn in enumerate(pages):
        for text, net, _ in pn.labels:
            on_net[(pi, net)].add(text.upper())
    nodes_of: dict[tuple, list[tuple]] = defaultdict(list)
    for node, texts in on_net.items():
        for t in texts:
            nodes_of[(node[0], t)].append(node)
    suspect: set[str] = set()
    for (_pi, t), nodes in nodes_of.items():
        others = [on_net[nd] - {t} for nd in nodes if len(on_net[nd]) > 1]
        if any(not (a & b) for i, a in enumerate(others) for b in others[i + 1:]):
            suspect.add(t)
    # ... but a text that alone names most of the nets it is on is a net name, even when it also
    # meets other names (aliases of one net drawn on several connectors)
    alone: Counter = Counter()
    uses: Counter = Counter()
    for node, texts in on_net.items():
        for t in texts & suspect:
            uses[t] += 1
            alone[t] += len(texts) == 1
    suspect = {t for t in suspect if 2 * alone[t] < uses[t]}
    bad: set[str] = set()
    if suspect:
        uf0 = UnionFind()
        first0: dict[str, tuple] = {}
        for node, texts in on_net.items():
            for t in texts - suspect:
                uf0.union(first0.setdefault(t, node), node)
        named = defaultdict(set)
        for node, texts in on_net.items():
            named[uf0.find(node)] |= texts - suspect
        by_text: dict[str, list[tuple]] = defaultdict(list)
        for (_pi, tt), nds in nodes_of.items():
            by_text[tt].extend(nds)
        for t in suspect:
            roots = {uf0.find(nd) for nd in by_text[t]}
            if sum(1 for r in roots if named[r]) > 1:
                bad.add(t)
    if bad:
        for pn in pages:
            pn.labels = [lab for lab in pn.labels if lab[0].upper() not in bad]
    uf = UnionFind()
    first_by_label: dict[str, tuple] = {}
    for pi, pn in enumerate(pages):
        for net in set(pn.seg_net):
            uf.find((pi, net))
        for text, net, _ in pn.labels:
            key = text.upper()
            node = (pi, net)
            if key in first_by_label:
                uf.union(first_by_label[key], node)
            else:
                first_by_label[key] = node
    # unnamed connector flags: a flag drawn exactly like flags that all carry one name is on that
    # net (a ground symbol with its name hidden); identical unnamed flags of one page share a net.
    # Never at the price of joining two differently named nets (a marker such as a power flag
    # sits on many nets)
    root_names: dict[tuple, set[str]] = defaultdict(set)
    for pi, pn in enumerate(pages):
        for text, net, _ in pn.labels:
            root_names[uf.find((pi, net))].add(text.upper())

    def join(a: tuple, b: tuple) -> None:
        ra, rb = uf.find(a), uf.find(b)
        na, nb = root_names.get(ra, set()), root_names.get(rb, set())
        if ra != rb and (not na or not nb or na == nb):
            uf.union(ra, rb)
            root_names[uf.find(ra)] = na | nb

    names_of: dict[tuple, set[str]] = defaultdict(set)
    anon: dict[tuple, list[tuple]] = defaultdict(list)
    for pi, pn in enumerate(pages):
        for t in pn.terminals:
            if t.comp is None and t.mark is not None:
                if not t.label:
                    anon[t.mark].append((pi, t.net))
                elif t.label.upper() not in bad:
                    names_of[t.mark].add(t.label.upper())
    for mark, nodes in anon.items():
        names = names_of.get(mark, set())
        if len(names) == 1:
            for nd in nodes:
                join(first_by_label[next(iter(names))], nd)
        elif not names:
            by_page: dict[int, list[tuple]] = defaultdict(list)
            for nd in nodes:
                by_page[nd[0]].append(nd)
            for nds in by_page.values():
                for nd in nds[1:]:
                    join(nds[0], nd)
    net_labels: dict[tuple, set[str]] = defaultdict(set)
    for pi, pn in enumerate(pages):
        for text, net, _ in pn.labels:
            net_labels[uf.find((pi, net))].add(text)
    label_nets = {k: uf.find(v) for k, v in first_by_label.items()}
    net_terms: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for pi, pn in enumerate(pages):
        for ti, t in enumerate(pn.terminals):
            net_terms[uf.find((pi, t.net))].append((pi, ti))
    return Design(pages, uf, dict(net_labels), label_nets, dict(net_terms))


class ExtractionCancelled(Exception):
    """Raised by extract_design when its cancel_check returns True."""


def _extract_pages(pdf_path: str, pnos: list[int], hint: tuple, scale: float) -> list[PageNetlist]:
    """Worker: netlists of some pages of one PDF (each process opens the file itself)."""
    import pdfplumber

    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno in pnos:
            page = pdf.pages[pno - 1]
            out.append(extract_page(page, pno, hint, scale))
            page.flush_cache()
    return out


def _refdes_color_votes(pages: Iterable[PageNetlist]) -> dict[tuple, int]:
    votes: dict[tuple, int] = defaultdict(int)
    for pn in pages:
        for w in pn.words:
            if w.role == "refdes" and SUFFIXED_REFDES_RE.match(w.text):
                votes[w.color] += 1
    return votes


def _cache_dir() -> Optional[Path]:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~")
    try:
        d = Path(base) / "SchematicNetlist" / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _cache_key(pdf_path: str, pages: Optional[list[int]]) -> str:
    """Hash of the PDF bytes, the page selection and the extraction code itself."""
    h = hashlib.sha1()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(repr(pages).encode())
    if getattr(sys, "frozen", False):  # packaged exe: the code version is the exe itself
        st = os.stat(sys.executable)
        h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    here = Path(__file__).parent
    for name in ("netlist.py", "image_extract.py"):
        try:
            h.update((here / name).read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()


def _available_memory_mb() -> Optional[int]:
    if os.name != "nt":
        return None
    import ctypes

    class _MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong)
            for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")
        ]

    st = _MemStatus()
    st.dwLength = ctypes.sizeof(st)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys // (1024 * 1024))


def default_workers(total_pages: int) -> int:
    """Physical cores (pdf parsing gains nothing from hyper-threads), at most 8, at least 4 pages
    per worker, and no more workers than free memory allows (~500 MB each incl. the libraries)."""
    cores = max(1, (os.cpu_count() or 2) // 2)
    n = max(1, min(cores, 8, total_pages // 4))
    free = _available_memory_mb()
    if free is not None:
        n = max(1, min(n, free // 500))
    return n


def extract_design(
    pdf_path: str,
    pages: Optional[Iterable[int]] = None,
    progress=None,
    cancel_check=None,
    workers: Optional[int] = None,
    use_cache: bool = True,
) -> Design:
    """Netlist of a whole PDF. Pages are read in parallel processes; the result is cached per
    PDF content + code version so a re-run with other options takes seconds."""
    import pdfplumber

    pages = list(pages) if pages else None
    cache_file = None
    if use_cache:
        cdir = _cache_dir()
        if cdir is not None:
            cache_file = cdir / f"{_cache_key(pdf_path, pages)}.pkl"
            if cache_file.exists():
                try:
                    with open(cache_file, "rb") as f:
                        design = pickle.load(f)
                    if progress:
                        progress(len(design.pages), len(design.pages))
                    return design
                except Exception:
                    pass

    with pdfplumber.open(pdf_path) as pdf:
        idxs = pages or list(range(1, len(pdf.pages) + 1))
    total = len(idxs)
    scale = document_scale(pdf_path, idxs)
    if workers is None:
        workers = default_workers(total)

    done: dict[int, PageNetlist] = {}
    if workers <= 1:
        color_votes: dict[tuple, int] = defaultdict(int)
        with pdfplumber.open(pdf_path) as pdf:
            for n, pno in enumerate(idxs, start=1):
                if cancel_check is not None and cancel_check():
                    raise ExtractionCancelled()
                hint = max(color_votes.items(), key=lambda kv: kv[1])[0] if color_votes else ()
                page = pdf.pages[pno - 1]
                done[pno] = extract_page(page, pno, hint, scale)
                for c, v in _refdes_color_votes([done[pno]]).items():
                    color_votes[c] += v
                page.flush_cache()
                if progress:
                    progress(n, total)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        chunks = [idxs[i::workers * 2] for i in range(workers * 2)]
        chunks = [c for c in chunks if c]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_extract_pages, pdf_path, c, (), scale): c for c in chunks}
            for fut in as_completed(futs):
                if cancel_check is not None and cancel_check():
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise ExtractionCancelled()
                for pn in fut.result():
                    done[pn.page] = pn
                if progress:
                    progress(len(done), total)
            # pages without a suffixed refdes of their own get the document-wide refdes colour
            votes = _refdes_color_votes(done.values())
            if votes:
                hint = max(votes.items(), key=lambda kv: kv[1])[0]
                redo = [p for p, pn in done.items() if pn.source != "raster" and not pn.refdes_color]
                if redo:
                    chunks = [redo[i::workers] for i in range(workers)]
                    for fut in as_completed([pool.submit(_extract_pages, pdf_path, c, hint, scale) for c in chunks if c]):
                        for pn in fut.result():
                            done[pn.page] = pn

    design = build_design([done[p] for p in idxs])
    design.scale = scale
    if cache_file is not None:
        try:
            with open(cache_file, "wb") as f:
                pickle.dump(design, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass
    return design
