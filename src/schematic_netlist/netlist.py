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

# All geometric tolerances below are in PDF points for a drawing printed at the reference scale;
# build_page() multiplies them by Primitives.scale = this document's text height / reference.
REF_CHAR_HEIGHT = 1.9  # document-wide median pdfium (loose) char box height of the reference export
TOL = 0.35  # geometric coincidence tolerance (pt)
DOT_MAX = 1.8  # max size of a junction dot (pt)
INTERNAL_MAX = 16.0  # max extent of a line group that is part of a symbol body (pt)
TINY_NET = 3.0  # a line group this small with both ends on symbol graphics is symbol body (pt)
PIN_GAP = 3.0  # max gap between a pin line's loose end and its symbol body (pt)
PART_GAP = 2.5  # anonymous symbol pieces this close to each other form one part (pt)
JOIN_GAP = 4.0  # an anonymous piece this close to a refdes'd symbol belongs to it (pt)
FAR_LABEL_MAX = 60.0  # max gap between an off-page connector symbol and its net-name text (pt)
FAR_LABEL_NEAR = 30.0  # beyond this gap the text needs a clear line of sight to the symbol (pt)
FAR_LABEL_OWN = 10.0  # text this close to another flag's wire end belongs to that flag, never to a far one (pt)
LOCAL_SPAN = 25.0  # max extent of one connector symbol along its pin direction (pt)

REFDES_RE = re.compile(r"^[A-Z]{1,3}\d{1,4}[A-Z]?(?:[_-]\d{1,3}[A-Z]?)?$")
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


@dataclass
class Component:
    page: int
    refdes: list[str]
    bbox: tuple[float, float, float, float]
    is_box: bool
    terminals: list[int] = field(default_factory=list)  # indices into PageNetlist.terminals
    # drawn body elements: "D" diagonal lines, "C" curves/circles, "B" boxes, "O" straight lines
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
        )


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
    colinear_tol: float = 0.05
    source: str = "vector"
    # scans lose junction dots: then a wire ending mid-way on a wire this long is a T-junction
    dotless_t_min: Optional[float] = None
    scale: float = 1.0  # median text height / REF_TEXT_HEIGHT: scales every geometric tolerance


# --------------------------------------------------------------------------- page extraction
def _color(obj: dict, key: str) -> tuple:
    c = obj.get(key)
    if isinstance(c, (list, tuple)):
        return tuple(round(float(v), 3) for v in c if isinstance(v, (int, float)))
    return ()


def document_scale(pdf_path: str, pages: Optional[list[int]] = None) -> float:
    """How much larger this document is drawn than the reference export (same schematic printed
    to a bigger sheet scales text and symbols alike). Median char height over the whole document,
    via pdfium (fast); deviations under 20% count as 1.0 so equal exports stay identical."""
    import pypdfium2 as pdfium

    hs: list[float] = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        for pno in pages or range(1, len(doc) + 1):
            tp = doc[pno - 1].get_textpage()
            n = tp.count_chars()
            for i in range(0, n, max(1, n // 200)):
                _l, b, _r, t = tp.get_charbox(i, loose=True)
                if 0.3 < t - b < 60:
                    hs.append(t - b)
    finally:
        doc.close()
    if len(hs) < 50:
        return 1.0
    s = sorted(hs)[len(hs) // 2] / REF_CHAR_HEIGHT
    if abs(s - 1.0) < 0.2:
        return 1.0
    return max(0.5, min(s, 3.0))


def _gather(page, dot_max: float = DOT_MAX) -> tuple[list, list, list, list, list]:
    orth: list[tuple[float, float, float, float]] = []
    diag: list[tuple[float, float, float, float]] = []
    curves: list[tuple[float, float, float, float]] = []
    boxes: list[tuple[float, float, float, float]] = []
    dots: list[tuple[float, float, float]] = []  # (x, y, r)

    def add_seg(a, b, body_only: bool = False) -> None:
        (x0, y0), (x1, y1) = a, b
        if abs(x0 - x1) < 0.05 and abs(y0 - y1) < 0.05:
            return
        if not body_only and (abs(x0 - x1) < 0.05 or abs(y0 - y1) < 0.05):
            if abs(x0 - x1) < 0.05:
                x = (x0 + x1) / 2
                orth.append((x, min(y0, y1), x, max(y0, y1)))
            else:
                y = (y0 + y1) / 2
                orth.append((min(x0, x1), y, max(x0, x1), y))
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
        if w <= dot_max and h <= dot_max and (has_curve or o.get("fill")):
            dots.append(((x0 + x1) / 2, (y0 + y1) / 2, max(w, h) / 2))
            continue
        pts = o.get("pts") or []
        closed = bool(pts) and len(pts) > 2 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.05
        if has_curve or closed or not path:
            curves.append((x0, y0, x1, y1))
            continue
        # open polyline: split into segments
        cur = None
        for op in path:
            if op[0] == "m":
                cur = op[1]
            elif op[0] == "l" and cur is not None:
                add_seg(cur, op[1])
                cur = op[1]

    pw, ph = float(page.width), float(page.height)
    for o in page.rects:
        x0, y0, x1, y1 = o["x0"], o["top"], o["x1"], o["bottom"]
        w, h = x1 - x0, y1 - y0
        dash = o.get("dash")
        if dash and dash[0]:
            continue  # dashed grouping frame, not a body
        if w > 0.6 * pw and h > 0.6 * ph:
            continue  # page border
        if w <= dot_max and h <= dot_max:
            dots.append(((x0 + x1) / 2, (y0 + y1) / 2, max(w, h) / 2))
            continue
        if w < 0.05 or h < 0.05:
            # degenerate rect = a line
            add_seg((x0, y0), (x1, y1))
            continue
        boxes.append((x0, y0, x1, y1))
    return orth, diag, curves, boxes, dots


def _box_edge_dist(px: float, py: float, b: tuple) -> float:
    """Distance from a point to the outline of a box."""
    inside = b[0] <= px <= b[2] and b[1] <= py <= b[3]
    if inside:
        return min(px - b[0], b[2] - px, py - b[1], b[3] - py)
    return _pt_bbox_dist(px, py, b)


def vector_primitives(page, scale: float = 1.0) -> Primitives:
    # text flow: a CAD label is one text object; y-clustering chains rows ~1.5 pt apart (tiny labels
    # between normal ones) and then breaks the labels on that "line" into single characters
    raw_words = page.extract_words(
        x_tolerance=1.5, y_tolerance=1.5, use_text_flow=True, extra_attrs=["non_stroking_color"]
    )
    words = [
        Word(w["text"], w["x0"], w["top"], w["x1"], w["bottom"], _color(w, "non_stroking_color"))
        for w in raw_words
        if w["text"].strip()
    ]
    orth, diag, curves, boxes, dots = _gather(page, DOT_MAX * scale)
    return Primitives(orth, diag, curves, boxes, dots, words, tol=TOL * scale, scale=scale)


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


def build_page(prims: Primitives, pno: int, pw: float, ph: float, refdes_color_hint: tuple = ()) -> PageNetlist:
    orth, diag, curves, boxes, dots, words = prims.orth, prims.diag, prims.curves, list(prims.boxes), prims.dots, prims.words
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
            area = (b[2] - b[0]) * (b[3] - b[1])
            for k in tg.query(*b):
                w = words[k]
                if b[0] - 0.5 <= w.x0 and w.x1 <= b[2] + 0.5 and b[1] - 0.5 <= w.top and w.bottom <= b[3] + 0.5:
                    if (w.x1 - w.x0) * (w.bottom - w.top) >= 0.2 * area:
                        return True
            return False

        boxes = [b for b in boxes if not is_frame(b)]

    n = len(orth)
    uf = UnionFind()
    for i in range(n):
        uf.find(i)
    sgrid = Grid()
    for i, (x0, y0, x1, y1) in enumerate(orth):
        sgrid.add(i, x0 - tol, y0 - tol, x1 + tol, y1 + tol)

    def horiz(i: int) -> bool:
        return abs(orth[i][1] - orth[i][3]) < 0.05

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

    # T-junctions at dots
    dot_at: list[tuple[float, float, float]] = dots
    dgrid = Grid()
    for k, (x, y, r) in enumerate(dots):
        dgrid.add(k, x - r, y - r, x + r, y + r)
        rr = max(r, tol) + 0.1
        hit = [j for j in sgrid.query(x - rr, y - rr, x + rr, y + rr) if _pt_seg_dist(x, y, *orth[j]) <= rr]
        for j in hit[1:]:
            uf.union(hit[0], j)

    def at_dot(px: float, py: float) -> bool:
        for k in dgrid.query(px - tol, py - tol, px + tol, py + tol):
            x, y, r = dot_at[k]
            if math.hypot(px - x, py - y) <= r + tol:
                return True
        return False

    # body elements: ("D", seg) diag, ("C", bbox) curve, ("B", bbox) box, ("O", orth idx)
    elems: list[tuple[str, tuple]] = []
    for s in diag:
        elems.append(("D", s))
    for b in curves:
        elems.append(("C", b))
    for b in boxes:
        elems.append(("B", b))

    def elem_bbox(e: tuple[str, tuple]) -> tuple:
        k, g = e
        if k == "D":
            return (min(g[0], g[2]), min(g[1], g[3]), max(g[0], g[2]), max(g[1], g[3]))
        if k == "O":
            return orth[g]
        return g

    def touches(e: tuple[str, tuple], px: float, py: float) -> bool:
        k, g = e
        if k == "D":
            return _pt_seg_dist(px, py, *g) <= tol
        if k == "C":
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
        if not t:
            step = 0.25
            while step <= pin_gap and not t:
                qx, qy = px + ux * step, py + uy * step
                t = [k for k in touched_elems(qx, qy) if elems[k][0] != "O"]
                step += 0.25
        if not t:
            continue
        terminals.append(Terminal(pno, px, py, i, uf.find(i), ux, uy, cluster=t[0]))

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
    # pin numbers: text right beside a terminal's pin line
    wgrid = Grid(10.0)
    for k, w in enumerate(words):
        wgrid.add(k, w.x0, w.top, w.x1, w.bottom)

    refdes_color = _dominant_color([w for w in words if SUFFIXED_REFDES_RE.match(w.text)]) or refdes_color_hint
    heights = sorted(w.bottom - w.top for w in words)
    # titles / sheet headings are much taller than the drawing's text and are never net labels
    max_label_h = 2.2 * heights[len(heights) // 2] if heights else 1e9
    cand: list[tuple[float, int, int]] = []
    for ti, t in enumerate(terminals):
        x0, y0, x1, y1 = orth[t.seg]
        for k in wgrid.query(x0 - 5 * S, y0 - 5 * S, x1 + 5 * S, y1 + 5 * S):
            w = words[k]
            if not PINNUM_RE.match(w.text) or (refdes_color and w.color == refdes_color and len(w.text) > 2):
                continue
            if VALUE_UNIT_RE.match(w.text) and not cl_isbox[t.cluster]:
                continue
            if abs(t.dy) < 0.5:  # horizontal pin line: number just above/below it
                if not (x0 - 1 <= w.cx <= x1 + 1):
                    continue
                gap = y0 - w.bottom if w.cy < y0 else w.top - y0
                if gap < -1.2 * S or gap > 3.5 * S:
                    continue
                score = abs(gap) + 0.1 * abs(w.cx - (x0 + x1) / 2)
            else:  # vertical pin line: number left/right of it
                if not (y0 - 1 <= w.cy <= y1 + 1):
                    continue
                gap = x0 - w.x1 if w.cx < x0 else w.x0 - x0
                if gap < -1.2 * S or gap > 3.5 * S:
                    continue
                score = abs(gap) + 0.1 * abs(w.cy - (y0 + y1) / 2)
            cand.append((score, ti, k))
    for _score, ti, k in sorted(cand):
        if terminals[ti].pin is None and not words[k].role:
            terminals[ti].pin = words[k].text
            words[k].role = "pin"

    # refdes -> symbol cluster: each refdes goes to its nearest symbol, preferring symbols it
    # sits above (the usual placement), so stacked units (J891A over J891B) stay apart
    cl_refdes: dict[int, list[str]] = defaultdict(list)
    pairs: list[tuple[float, int, int]] = []
    # without a refdes colour, text repeated on the page is a part value (AGN2004H), not a refdes
    repeats = Counter(w.text for w in words) if not refdes_color else Counter()
    for k, w in enumerate(words):
        if w.role or not REFDES_RE.match(w.text) or repeats[w.text] > 2:
            continue
        if refdes_color and w.color and w.color != refdes_color:
            continue
        for r in term_clusters:
            bb = cl_bbox[r]
            if _bbox_gap(w.bbox, tuple(bb)) >= 15.0 * S:
                continue
            d = word_cluster_dist(w.bbox, r)
            if d < 15.0 * S:
                if w.top > bb[1] + 1.0:
                    d += 1.5 * S  # not above the symbol
                pairs.append((d, k, r))
    pairs.sort()
    best_of: dict[int, tuple[float, int]] = {}
    for d, k, r in pairs:
        best_of.setdefault(k, (d, r))
    for k, (_d, r) in sorted(best_of.items(), key=lambda kv: kv[1][0]):
        cl_refdes[r].append(words[k].text)
        words[k].role = "refdes"

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
    terms_by_cl = defaultdict(list)
    for ti, t in enumerate(terminals):
        terms_by_cl[t.cluster].append(ti)
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
                if (t.dx < 0 and not (t.x - local_span <= b[2] and b[0] <= t.x + 0.5)) or (
                    t.dx > 0 and not (t.x - 0.5 <= b[2] and b[0] <= t.x + local_span)
                ):
                    continue
            else:
                if b[2] < t.x - 3 * S or b[0] > t.x + 3 * S:
                    continue
                if (t.dy < 0 and not (t.y - local_span <= b[3] and b[1] <= t.y + 0.5)) or (
                    t.dy > 0 and not (t.y - 0.5 <= b[3] and b[1] <= t.y + local_span)
                ):
                    continue
            out = list(b) if out is None else [min(out[0], b[0]), min(out[1], b[1]), max(out[2], b[2]), max(out[3], b[3])]
        return out

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
        shape = dict(Counter(kind for kind, _ in cl_elems.get(r, [])))
        comps.append(Component(pno, sorted(set(refs)), tuple(cl_bbox[r]), cl_isbox[r], shape=shape))
    for ti, t in enumerate(terminals):
        ci = cl_comp.get(t.cluster)
        if ci is not None:
            t.comp = ci
            comps[ci].terminals.append(ti)

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
                if w.role or not (b[0] - 0.5 <= w.cx <= b[2] + 0.5 and b[1] - 0.5 <= w.cy <= b[3] + 0.5):
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
            if 0 <= y0 - w.bottom <= 2.0 * S and x0 - 0.5 <= w.x0 and w.x1 <= x1 + 0.5:
                w.role = "label"
                labels.append((w.text, uf.find(j), w))
                break

    seg_net = [uf.find(i) for i in range(n)]
    return PageNetlist(pno, pw, ph, orth, seg_net, terminals, comps, labels, words, prims.source, refdes_color)


def _dominant_color(words: list[Word]) -> tuple:
    counts: dict[tuple, int] = defaultdict(int)
    for w in words:
        counts[w.color] += 1
    if not counts:
        return ()
    c, k = max(counts.items(), key=lambda kv: kv[1])
    return c if k >= 3 else ()


def _labelish(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text)) and not VALUE_RE.match(text) and not PINNUM_RE.match(text)


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
            if w.role or abs(w.cy - t.y) > 2.6 * S or not _labelish(w.text) or w.bottom - w.top > max_h:
                continue
            d = bb[0] - w.x1 if t.dx < 0 else w.x0 - bb[2]
            score = d + abs(w.cy - t.y)
            if not (-1.0 <= d and score < bd) or (blocked is not None and blocked(k, score)):
                continue
            if d > far_near and clear is not None:
                corridor = (w.x1 + 0.3, t.y - 0.8, bb[0] - 0.3, t.y + 0.8) if t.dx < 0 else (bb[2] + 0.3, t.y - 0.8, w.x0 - 0.3, t.y + 0.8)
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
            if w.role or not (w.x0 - 2 <= t.x <= w.x1 + 2) or not _labelish(w.text) or w.bottom - w.top > max_h:
                continue
            d = bb[1] - w.bottom if t.dy < 0 else w.top - bb[3]
            if not (-1.0 <= d < 12.0 * S):
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
