"""Raster front-end for schematic pages without vector drawing (scans, flattened exports).

Recovers from pixels the same primitives the vector path reads from the PDF -- orthogonal
wire segments, junction dots, symbol blobs, boxes and words -- so `netlist.build_page`
treats both kinds of page identically. Needs the `image` extra (opencv-python-headless,
rapidocr + onnxruntime, numpy). OCR is RapidOCR with the PP-OCRv6 models that ship inside
its package: nothing to install or download.
"""
from __future__ import annotations

import re

from .netlist import DOT_MAX, Primitives, Word

_OCR_MIN_SCORE = 0.5  # RapidOCR line confidence below this is noise (symbol strokes read as letters)
OCR_TILE_PX = 1600  # RapidOCR shrinks anything over 2000 px: a page is read in overlapping tiles
OCR_TILE_OVERLAP_PX = 240  # more than the longest label is tall, so every token lies whole in some tile
_TOKEN_TRIM = re.compile(r"^[^A-Za-z0-9+]+|[^A-Za-z0-9)%]+$")
_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})

RASTER_TOL = 1.0  # pixel-quantized coordinates need a looser coincidence tolerance (pt)
MIN_WIRE_PT = 1.9  # shortest stroke kept as a wire: above the thickest wire width, below pin stubs (~2.1 pt)
MAX_WIRE_THICK_PT = 2.5  # thicker orthogonal runs are filled shapes, not wires
THICK_WIRE_PT = 0.9  # a junction dot is invisible on a wire at least this thick
DOTLESS_T_MIN_PT = 8.0  # a wire ending mid-way on a wire this long is joined to it (scans lose dots)
OCR_ERASE_PT = 6.0  # strokes at least this long are wires, erased before OCR
GLYPH_MAX_PT = 5.0  # connected ink no taller/wider than this, in a row of such, is text
DASH_GAP_PT = 3.0  # dashed lines have gaps up to this long
DASH_MIN_LINE_PT = 12.0  # ... and run at least this far
DASH_SEG_MAX_PT = 4.0  # every dash of a dashed line is at most this long
DASH_MAX_THICK_PT = 1.2  # ... and the line at most this thick
FRAGMENT_MAX_PT = 3.0  # longest run that can be a staircase step of a diagonal stroke
COMPACT_MAX_PT = 12.0  # isolated ink no larger than this (a relay's contact group) ...
COMPACT_MIN_PT = 1.2  # ... and at least this wide both ways (not a straight stub) is a symbol shape
REGION_MIN_W_PT = 24.0  # smallest embedded image (drawn size) that can hold readable text
REGION_MIN_H_PT = 12.0
REGION_MIN_COVER = 0.03  # ... and it must cover at least this share of the page
LOGO_MIN_PAGES = 3  # an image with the same pixel size on this many pages is a logo/stamp, never OCR'd
# region class (_region_kind): measured on the image's own pixels, never on its drawn size
REGION_CLASS_DPI = 150  # an embedded image is measured as a scan of this density (1 image pixel = 0.48 pt)
REGION_CLASS_MAX_PX = 12_000_000  # bigger images are measured downsampled (bound by pixel count)
REGION_DRAWING_MIN_DOTS = 3  # filled junction dots that a drawing needs (tables/text have none)
REGION_DRAWING_MAX_DOTS_PER_WIRE = 0.33  # schematics 0.07-0.27; pads/glyph residue of tables, pinouts: 0.4-5
REGION_DRAWING_MIN_LONG_SHARE = 0.05  # share of wires >= REGION_LONG_WIRE_PT: schematics >= 0.09, ball maps 0.02
REGION_LONG_WIRE_PT = 10.0
REGION_RULED_MAX = 0.5  # share of line length in lines spanning half the picture: tables ~1, schematics <= 0.47
REGION_TABLE_BOX_COVER = 0.3  # boxes (below half the picture: no frame) covering this much = cells/legend, not a drawing


def _require_image_deps() -> None:
    missing = []
    for mod, pkg in (("cv2", "opencv-python-headless"), ("rapidocr", "rapidocr"), ("onnxruntime", "onnxruntime"), ("numpy", "numpy")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise RuntimeError(
            "This page has no vector drawing (scanned/flattened), and reading it needs: "
            + ", ".join(missing)
            + '.\nInstall with:  uv pip install -e ".[image]"'
        )


_ENGINE = None


def ocr_engine():
    """The RapidOCR engine (PP-OCRv6 detection + recognition), loaded once per process."""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR

        _ENGINE = RapidOCR(params={"Global.log_level": "critical"})
    return _ENGINE


def page_is_raster(page) -> bool:
    """True when a page has (almost) no vector drawing but an image covering much of it."""
    if len(page.lines) + len(page.rects) + len(page.curves) > 10 or not page.images:
        return False
    pw, ph = float(page.width), float(page.height)
    covered = 0.0
    for im in page.images:
        w = min(float(im["x1"]), pw) - max(float(im["x0"]), 0.0)
        h = min(float(im["bottom"]), ph) - max(float(im["top"]), 0.0)
        covered += max(w, 0.0) * max(h, 0.0)
    return covered >= 0.3 * pw * ph


def text_is_outlined(page, words: list[Word]) -> bool:
    """A real vector drawing whose text is drawn as outlines (no text layer): needs OCR for words."""
    drawing = len(page.lines) + len(page.rects) + len(page.curves)
    readable = sum(1 for w in words if re.search(r"[A-Za-z0-9]", w.text))
    return drawing >= 50 and readable < 5


def ocr_page_words(page, resolution: int = 300) -> list[Word]:
    """Words of a rendered page via OCR (for vector pages without a text layer)."""
    _require_image_deps()
    import numpy as np

    gray = np.array(page.to_image(resolution=resolution).original.convert("L"))
    return _ocr_words(gray, resolution / 72.0)


# --------------------------------------------------------------------------- words
def _clean_token(text: str) -> str:
    t = _TOKEN_TRIM.sub("", text.strip())
    if len(t) >= 4 and sum(c.isdigit() for c in t) >= 0.75 * len(t):
        return t.translate(_OCR_DIGIT_FIX)
    # the short hyphen after a pogo block number reads as a dot or comma: 416.P16 -> 416-P16
    t = re.sub(r"^(\d{3})[.,·•](?=[A-Z])", r"\1-", t)
    # O/0 inside codes such as 893-F03OUT1: a zero before two letters is an O, an O next to a digit a zero
    t = re.sub(r"0(?=[A-Z]{2})", "O", t)
    t = re.sub(r"(?<=[A-Z])O(?=\d)|(?<=\d)O(?=\d)|(?<=\d)O\b|(?<=\d)O(?=OUT\d)", "0", t)
    # a pin code must end in its channel number: 431-HFS -> 431-HF5
    m = re.match(r"^(\d{3}-[A-Z]{1,2})([SOIlBZ])$", t)
    if m:
        t = m.group(1) + m.group(2).translate(_OCR_DIGIT_FIX)
    return t


def _text_layer_words(page) -> list[Word]:
    if not page.chars:
        return []
    return [
        Word(w["text"], w["x0"], w["top"], w["x1"], w["bottom"])
        for w in page.extract_words(x_tolerance=1.5, y_tolerance=1.5)
        if w["text"].strip()
    ]


def _line_tokens(text: str, x0: float, x1: float) -> list[tuple[str, float, float]]:
    """A read line split at its spaces; each token's span is proportional to its characters."""
    n = max(len(text), 1)
    return [(m.group(), x0 + (x1 - x0) * m.start() / n, x0 + (x1 - x0) * m.end() / n) for m in re.finditer(r"\S+", text)]


def _ocr_words(gray, scale: float) -> list[Word]:
    """Words of one rendered image (a page or a picture region), in pt. RapidOCR reads the raw
    image: erasing wires or binarizing first loses text on coloured table cells. Big images are
    read in overlapping tiles; a token cut by a tile edge is taken from the tile holding it whole.
    Vertical text needs no second pass: the detector finds it and the recognizer turns it."""
    import cv2

    engine = ocr_engine()
    H, W = gray.shape
    T, O = OCR_TILE_PX, OCR_TILE_OVERLAP_PX
    found: list[Word] = []
    by_text: dict[str, list[Word]] = {}
    for ty in range(0, max(1, H - O), T - O):
        for tx in range(0, max(1, W - O), T - O):
            tile = gray[ty:ty + T, tx:tx + T]
            if tile.size == 0 or tile.min() > 200:
                continue  # blank paper
            th, tw = tile.shape
            res = engine(cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR))
            if res.txts is None:
                continue
            for txt, sc, box in zip(res.txts, res.scores, res.boxes):
                if sc < _OCR_MIN_SCORE:
                    continue
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                # the detector's line box carries a margin (measured: 0.18 of its height above and on
                # each side, 0.06 below); trim it to the glyphs, like the boxes of a text layer
                bh = max(ys) - min(ys)
                ly0, ly1 = min(ys) + ty + 0.18 * bh, max(ys) + ty - 0.06 * bh
                for tok, a, b in _line_tokens(txt, min(xs) + tx + 0.18 * bh, max(xs) + tx - 0.18 * bh):
                    cut = (a - tx < 3 and tx > 0) or (ly0 - ty < 3 and ty > 0) or (tx + tw - b < 3 and tx + tw < W) or (ty + th - ly1 < 3 and ty + th < H)
                    text = _clean_token(tok)
                    if cut or not text:
                        continue
                    w = Word(text, a / scale, ly0 / scale, b / scale, ly1 / scale)
                    # tiles overlap: the same token read by two tiles counts once
                    same = by_text.setdefault(text, [])
                    if not any(_overlap(v, w) > 0.5 for v in same):
                        same.append(w)
                        found.append(w)
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return _join_underscores(found, ink, scale)


def _overlap(a: Word, b: Word) -> float:
    """Intersection over the smaller of two word boxes."""
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.bottom, b.bottom) - max(a.top, b.top)
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(1e-9, min((a.x1 - a.x0) * (a.bottom - a.top), (b.x1 - b.x0) * (b.bottom - b.top)))


def _ocr_rows(gray, rows: list[tuple[int, int, int, int]], scale: float) -> list[Word]:
    """Second OCR pass: each text row the page-level pass missed, cropped and read as one line
    (recognition only, no detection)."""
    import cv2

    engine = ocr_engine()
    pad = int(1.0 * scale)
    H, W = gray.shape
    out: list[Word] = []
    for x0, y0, x1, y1 in rows:
        cx0, cy0, cx1, cy1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
        crop = gray[cy0:cy1, cx0:cx1]
        if crop.size == 0:
            continue
        res = engine(cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR), use_det=False, use_cls=False)
        for txt, sc in zip(res.txts or (), res.scores or ()):
            if sc < _OCR_MIN_SCORE:
                continue
            for tok, a, b in _line_tokens(txt, x0, x1):
                text = _clean_token(tok)
                if len(text) >= 3:
                    out.append(Word(text, a / scale, y0 / scale, b / scale, y1 / scale))
    return out


def _join_underscores(words: list[Word], ink, scale: float) -> list[Word]:
    """OCR may drop an underscore and split there (J2_1 -> J2, 1): rejoin neighbours on one
    line whose gap holds a baseline stroke."""
    H, W = ink.shape
    words = sorted(words, key=lambda w: (round(w.top), w.x0))
    out: list[Word] = []
    for w in words:
        prev = out[-1] if out else None
        if prev is not None:
            h = max(prev.bottom - prev.top, w.bottom - w.top)
            same_line = min(prev.bottom, w.bottom) - max(prev.top, w.top) >= 0.5 * h
            gap = w.x0 - prev.x1
            if same_line and -0.3 <= gap <= 1.2 * h:
                y0 = int((max(prev.bottom, w.bottom) - 0.3 * h) * scale)
                y1 = min(H, int((max(prev.bottom, w.bottom) + 0.5 * h) * scale) + 1)
                x0, x1 = max(0, int(prev.x1 * scale)), min(W, int(w.x0 * scale) + 1)
                band = ink[y0:y1, x0:x1]
                if band.size and x1 > x0 and (band > 0).any(axis=0).mean() >= 0.6:
                    out[-1] = Word(f"{prev.text}_{w.text}", prev.x0, min(prev.top, w.top), w.x1, max(prev.bottom, w.bottom))
                    continue
        out.append(w)
    return out


# --------------------------------------------------------------------------- geometry
def _runs(mask, scale: float, horizontal: bool):
    """Components of a directional stroke mask -> ([x0, y0, x1, y1, thickness_px] runs, filled blobs)."""
    import cv2

    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    min_len = MIN_WIRE_PT * scale
    max_thick = MAX_WIRE_THICK_PT * scale
    runs, blobs = [], []
    for i in range(1, n):
        x, y, w, h, _area = stats[i]
        length, thick = (w, h) if horizontal else (h, w)
        if length < min_len:
            continue
        if thick > max_thick:
            blobs.append((x, y, x + w, y + h))
        elif horizontal:
            c = y + (h - 1) / 2
            runs.append([float(x), c, float(x + w - 1), c, float(thick)])
        else:
            c = x + (w - 1) / 2
            runs.append([c, float(y), c, float(y + h - 1), float(thick)])
    return runs, blobs


def _snap_ends(hr: list, vr: list, thick_px: float) -> list[tuple[float, float]]:
    """Move run ends that stop inside a perpendicular stroke onto its centre line (the two strokes'
    pixels overlap). Returns points where an end meets a thick stroke mid-way: junctions whose dot
    is hidden by the stroke width."""
    junctions: list[tuple[float, float]] = []
    for runs, others, horiz in ((hr, vr, True), (vr, hr, False)):
        for r in runs:
            for end in (0, 1):
                px, py = (r[0], r[1]) if end == 0 else (r[2], r[3])
                along, across = (py, px) if horiz else (px, py)
                for o in others:
                    half = o[4] / 2 + 1.0
                    o_pos, o_lo, o_hi = (o[0], o[1], o[3]) if horiz else (o[1], o[0], o[2])
                    if abs(across - o_pos) <= half and o_lo - 1 <= along <= o_hi + 1:
                        if horiz:
                            r[0 if end == 0 else 2] = o_pos
                        else:
                            r[1 if end == 0 else 3] = o_pos
                        if o[4] >= thick_px and o_lo + half < along < o_hi - half:
                            junctions.append((o_pos, py) if horiz else (px, o_pos))
                        break
    return junctions


def _boxes(hr: list, vr: list, scale: float) -> list[tuple[float, float, float, float]]:
    """Rectangles (symbol bodies) in px: two vertical runs of equal span closed by horizontal
    runs at top and bottom. Built from runs, so clutter inside the box does not matter."""
    t = max(2.0, 0.6 * scale)
    min_side = 4.0 * scale
    by_y: dict[int, list] = {}
    for h in hr:
        by_y.setdefault(int(h[1] // t), []).append(h)

    def edge(y: float, x0: float, x1: float) -> bool:
        for key in (int(y // t) - 1, int(y // t), int(y // t) + 1):
            for h in by_y.get(key, ()):
                if abs(h[1] - y) <= t and h[0] <= x0 + t and h[2] >= x1 - t:
                    return True
        return False

    vs = sorted(vr, key=lambda v: v[0])
    out = []
    for i, L in enumerate(vs):
        if L[3] - L[1] < min_side:
            continue
        for R in vs[i + 1:]:
            if R[0] - L[0] < min_side:
                continue
            if abs(R[1] - L[1]) <= t and abs(R[3] - L[3]) <= t and edge(L[1], L[0], R[0]) and edge(L[3], L[0], R[0]):
                out.append((L[0], min(L[1], R[1]), R[0], max(L[3], R[3])))
                break
    return out


def _on_outline(seg: tuple, b: tuple, tol: float = 1.0) -> bool:
    x0, y0, x1, y1 = seg
    if abs(y0 - y1) < 1e-6:
        return (abs(y0 - b[1]) <= tol or abs(y0 - b[3]) <= tol) and x0 >= b[0] - tol and x1 <= b[2] + tol
    return (abs(x0 - b[0]) <= tol or abs(x0 - b[2]) <= tol) and y0 >= b[1] - tol and y1 <= b[3] + tol


def native_resolution(page, default: int = 300) -> int:
    """DPI of the page's largest embedded image (render at scan resolution, no resampling blur)."""
    best = None
    for im in page.images:
        w_pt = float(im["x1"]) - float(im["x0"])
        src = im.get("srcsize")
        if src and w_pt > 0 and (best is None or w_pt > best[0]):
            best = (w_pt, src[0] / (w_pt / 72.0))
    dpi = int(round(best[1])) if best else default
    return max(150, min(dpi, 600))


def _erase_dashed(ink, scale: float):
    """Remove dashed lines (grouping frames): close the dash gaps along each direction, then
    drop long closed runs whose original ink is only partly filled."""
    import cv2
    import numpy as np

    close = max(3, int(DASH_GAP_PT * scale))
    min_len = DASH_MIN_LINE_PT * scale
    out = ink.copy()
    for horizontal in (True, False):
        kc = (close, 1) if horizontal else (1, close)
        ko = (int(min_len), 1) if horizontal else (1, int(min_len))
        closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, kc))
        lines = cv2.morphologyEx(closed, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, ko))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(lines, connectivity=8)
        dash_max = DASH_SEG_MAX_PT * scale
        for i in range(1, n):
            x, y, w, h, _a = stats[i]
            if (h if horizontal else w) > DASH_MAX_THICK_PT * scale:
                continue  # stacked symbols, not a thin dashed stroke
            region = ink[y:y + h, x:x + w] > 0
            profile = region.any(axis=0) if horizontal else region.any(axis=1)
            edges = np.flatnonzero(np.diff(np.concatenate(([0], profile.astype(np.int8), [0]))))
            seg_lens = edges[1::2] - edges[0::2]
            # judged by length: crossing wires merge an occasional pair of dashes, and a solid
            # wire has only a few short bits at its ends
            short_share = seg_lens[seg_lens <= dash_max].sum() / max(seg_lens.sum(), 1)
            if len(seg_lens) >= 4 and short_share >= 0.7:
                mask = (lab[y:y + h, x:x + w] == i)
                mask = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
                out[y:y + h, x:x + w][mask] = 0
    return out


def _text_components(ink, words: list[Word], scale: float):
    """(mask, unread_rows): mask of glyph-sized ink blobs that are text -- inside a word box of
    >= 3 characters (a misread symbol is usually 1-2), or in a row of >= 3 such blobs -- and the
    px bboxes of those rows no OCR word covers. Larger blobs under a word box are symbols OCR
    hallucinated text on, and are kept."""
    import cv2
    import numpy as np

    n, lab, stats, cents = cv2.connectedComponentsWithStats(ink, connectivity=8)
    gmax, gap = GLYPH_MAX_PT * scale, 1.2 * scale
    glyphs = [i for i in range(1, n) if stats[i][2] <= gmax and stats[i][3] <= gmax and stats[i][4] >= 3]
    boxes = [(w.x0 * scale, w.top * scale, w.x1 * scale, w.bottom * scale) for w in words if len(w.text) >= 3]
    text_ids: set[int] = set()
    for i in glyphs:
        cx, cy = cents[i]
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes):
            text_ids.add(i)
    covered = set(text_ids)

    # unread text: rows of >= 3 blobs of capital-letter height (symbol pieces such as contact
    # circles are smaller) sharing a baseline
    heights = sorted(stats[i][3] for i in text_ids)
    cap = heights[len(heights) // 2] if heights else 3.0 * scale
    comps = sorted((i for i in glyphs if 0.75 * cap <= stats[i][3] <= 1.25 * cap), key=lambda i: stats[i][0])
    parent = {i: i for i in comps}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for k, i in enumerate(comps):
        x, y, w, h = stats[i][:4]
        for j in comps[k + 1:]:
            x2, y2, w2, h2 = stats[j][:4]
            if x2 > x + w + gap:
                break
            if abs((y + h) - (y2 + h2)) <= 0.25 * cap:
                parent[find(j)] = find(i)
    rows: dict[int, list[int]] = {}
    for i in comps:
        rows.setdefault(find(i), []).append(i)
    unread = []
    for members in rows.values():
        if len(members) >= 3:
            text_ids.update(members)
            if not any(m in covered for m in members):
                unread.append((
                    min(stats[m][0] for m in members), min(stats[m][1] for m in members),
                    max(stats[m][0] + stats[m][2] for m in members), max(stats[m][1] + stats[m][3] for m in members),
                ))
    return np.isin(lab, list(text_ids)).astype(np.uint8) * 255, unread


def _drop_diagonal_fragments(ink, runs: list, horizontal: bool, scale: float) -> list:
    """Drop short runs that are staircase steps of a diagonal stroke: at both ends the ink carries
    on sideways (diagonally) instead of straight ahead."""
    H, W = ink.shape
    max_len = FRAGMENT_MAX_PT * scale
    reach = max(2, int(0.6 * scale))

    def ink_at(x: float, y: float) -> bool:
        xi, yi = int(round(x)), int(round(y))
        return 0 <= xi < W and 0 <= yi < H and ink[yi, xi] > 0

    def diagonal_end(px: float, py: float, dx: int, dy: int) -> bool:
        ahead = [(px + dx * s, py + dy * s) for s in range(2, reach + 2)]
        if any(ink_at(x, y) for x, y in ahead):
            return False
        side = []
        for s in range(1, reach + 1):
            for o in range(2, reach + 2):
                side += [(px + dx * s + dy * o, py + dy * s + dx * o), (px + dx * s - dy * o, py + dy * s - dx * o)]
        return any(ink_at(x, y) for x, y in side)

    kept = []
    for r in runs:
        length = r[2] - r[0] if horizontal else r[3] - r[1]
        if length <= max_len:
            if horizontal:
                ends = ((r[0], r[1], -1, 0), (r[2], r[3], 1, 0))
            else:
                ends = ((r[0], r[1], 0, -1), (r[2], r[3], 0, 1))
            if all(diagonal_end(*e) for e in ends):
                continue
        kept.append(r)
    return kept


def raster_primitives(page, resolution: int | None = None) -> Primitives:
    _require_image_deps()
    import numpy as np

    resolution = resolution or native_resolution(page)
    scale = resolution / 72.0
    gray = np.array(page.to_image(resolution=resolution).original.convert("L"))
    words = _text_layer_words(page) or _ocr_words(gray, scale)
    return _primitives_from_gray(gray, scale, words, has_text_layer=bool(page.chars))


def _primitives_from_gray(gray, scale: float, words: list[Word], has_text_layer: bool) -> Primitives:
    """Primitives of one rendered image (a scanned page or an embedded picture region)."""
    import cv2
    import numpy as np

    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # text must not become wires or symbols; long strokes (wires running under a label) stay
    words = list(words)
    kl = max(3, int(OCR_ERASE_PT * scale))
    long_strokes = cv2.bitwise_or(
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (kl, 1))),
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, kl))),
    )
    text, unread = _text_components(cv2.bitwise_and(ink, cv2.bitwise_not(long_strokes)), words, scale)
    if unread and not has_text_layer:
        words += _ocr_rows(gray, unread, scale)
    ink = cv2.bitwise_and(ink, cv2.bitwise_not(text))
    ink = _erase_dashed(ink, scale)

    # compact closed shapes (contact circles, arrowheads) are symbol graphics, not wire pieces
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    cmax, cmin = COMPACT_MAX_PT * scale, COMPACT_MIN_PT * scale
    compact = [i for i in range(1, n) if max(stats[i][2], stats[i][3]) <= cmax and min(stats[i][2], stats[i][3]) >= cmin]
    compact_blobs = [(stats[i][0], stats[i][1], stats[i][0] + stats[i][2], stats[i][1] + stats[i][3]) for i in compact]
    ink = cv2.bitwise_and(ink, cv2.bitwise_not(np.isin(lab, compact).astype(np.uint8) * 255))

    k = max(3, int(round(MIN_WIRE_PT * scale)))
    hmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    vmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
    hr, hblobs = _runs(hmask, scale, True)
    vr, vblobs = _runs(vmask, scale, False)
    hr = _drop_diagonal_fragments(ink, hr, True, scale)
    vr = _drop_diagonal_fragments(ink, vr, False, scale)
    junctions = _snap_ends(hr, vr, THICK_WIRE_PT * scale)

    # junction dots: small round filled blobs, wider than thin strokes, where >= 2 wires meet
    dk = max(3, int(round(0.55 * scale)))
    dot_mask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dk, dk)))
    n, labels_img, stats, cents = cv2.connectedComponentsWithStats(dot_mask, connectivity=8)
    dot_px = DOT_MAX * scale
    runs_px = hr + vr

    def wires_near(cx: float, cy: float, r: float) -> int:
        return sum(1 for s in runs_px if s[0] - r <= cx <= s[2] + r and s[1] - r <= cy <= s[3] + r)

    dots = []
    dot_ids = []
    for i in range(1, n):
        w, h = stats[i][2], stats[i][3]
        if w > dot_px or h > dot_px or not 0.6 <= w / max(h, 1) <= 1.7:
            continue
        cx, cy = cents[i]
        if wires_near(cx, cy, max(w, h) / 2 + 1) >= 2:
            dots.append((cx / scale, cy / scale, max(w, h) / 2 / scale))
            dot_ids.append(i)
    dots.extend((x / scale, y / scale, 0.5) for x, y in junctions)
    accepted_dots = np.isin(labels_img, dot_ids).astype(np.uint8) * 255

    # symbol bodies: closed rectangles, plus every ink blob that is neither stroke, dot nor text
    lines = cv2.bitwise_or(hmask, vmask)
    boxes = [(x0 / scale, y0 / scale, x1 / scale, y1 / scale) for x0, y0, x1, y1 in _boxes(hr, vr, scale)]
    rest = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.dilate(lines, np.ones((3, 3), np.uint8))))
    rest = cv2.bitwise_and(rest, cv2.bitwise_not(accepted_dots))
    n, _, stats, _ = cv2.connectedComponentsWithStats(rest, connectivity=8)
    curves = [
        (stats[i][0] / scale, stats[i][1] / scale, (stats[i][0] + stats[i][2]) / scale, (stats[i][1] + stats[i][3]) / scale)
        for i in range(1, n)
        if stats[i][4] >= 4
    ]
    curves.extend((x0 / scale, y0 / scale, x1 / scale, y1 / scale) for x0, y0, x1, y1 in hblobs + vblobs + compact_blobs)

    orth = []
    for r in hr + vr:
        seg = (r[0] / scale, r[1] / scale, r[2] / scale, r[3] / scale)
        if not any(_on_outline(seg, b) for b in boxes):
            orth.append(seg)
    return Primitives(
        orth, [], curves, boxes, dots, words, tol=RASTER_TOL, colinear_tol=0.6, source="raster", dotless_t_min=DOTLESS_T_MIN_PT,
        drawn_dots=len(dot_ids),
    )


# --------------------------------------------------------------------------- embedded image regions
def image_regions(page, logo_sizes: frozenset = frozenset()) -> list[tuple[float, float, float, float]]:
    """Embedded raster regions of a vector page big enough to hold readable content: pictures of
    tables (tester-configuration sheets) or drawings pasted into the schematic. Repeated logos
    (same pixel size on several pages: ``logo_sizes``) and tiny stamps are skipped; the same
    image drawn several times at one spot counts once."""
    pw, ph = float(page.width), float(page.height)
    seen: set = set()
    out: list[tuple[float, float, float, float]] = []
    for im in page.images:
        x0, top = max(0.0, float(im["x0"])), max(0.0, float(im["top"]))
        x1, bottom = min(pw, float(im["x1"])), min(ph, float(im["bottom"]))
        w, h = x1 - x0, bottom - top
        if tuple(im.get("srcsize") or ()) in logo_sizes:
            continue
        if w < REGION_MIN_W_PT or h < REGION_MIN_H_PT or w * h < REGION_MIN_COVER * pw * ph:
            continue
        key = (round(x0, 1), round(top, 1), round(x1, 1), round(bottom, 1))
        if key not in seen:
            seen.add(key)
            out.append((x0, top, x1, bottom))
    return out


def _region_native_dpi(page, bbox: tuple[float, float, float, float]) -> float | None:
    """Pixel density of the largest embedded image inside ``bbox`` as drawn (image pixels per
    inch of page); None when no image there reports its pixel size."""
    x0, top, x1, bottom = bbox
    best = None
    for im in page.images:
        w_pt = float(im["x1"]) - float(im["x0"])
        src = im.get("srcsize")
        inside = float(im["x0"]) >= x0 - 1 and float(im["x1"]) <= x1 + 1 and float(im["top"]) >= top - 1 and float(im["bottom"]) <= bottom + 1
        if inside and src and w_pt > 0 and (best is None or w_pt > best[0]):
            best = (w_pt, src[0] / (w_pt / 72.0))
    return best[1] if best else None


def _region_dpi(page, bbox: tuple[float, float, float, float], default: int = 300) -> int:
    """Render density for OCR of one region: its native pixel density (same rule as
    native_resolution, but per region), clamped to 150-600."""
    native = _region_native_dpi(page, bbox)
    dpi = int(round(native)) if native else default
    return max(150, min(dpi, 600))


def _region_kind(page, bbox: tuple[float, float, float, float], words: list[Word]) -> str:
    """Class of one embedded image -- "drawing" | "table" | "text" -- decided on the image's OWN
    pixels so a picture gets the same class whatever size it is drawn at: rendered at its native
    density (one image pixel = one pixel), measured as a REGION_CLASS_DPI scan, text erased
    (``words``: its OCR words in region pt). A drawing (a pasted schematic) has filled junction
    dots, few of them per wire, a share of long wires, and is neither ruled (lines spanning the
    picture: a table) nor covered by boxes (cells, legend swatches); everything else with lines
    is a table."""
    import math

    import numpy as np

    native = _region_native_dpi(page, bbox) or float(REGION_CLASS_DPI)
    x0, top, x1, bottom = bbox
    px = (x1 - x0) * (bottom - top) * (native / 72.0) ** 2
    if px > REGION_CLASS_MAX_PX:  # bounded by the image's pixel count, not by its drawn size
        native *= math.sqrt(REGION_CLASS_MAX_PX / px)
    sc = REGION_CLASS_DPI / 72.0
    k = native / 72.0 / sc  # region pt -> measurement units
    gray = np.array(page.crop(bbox).to_image(resolution=native).original.convert("L"))
    mapped = [Word(w.text, w.x0 * k, w.top * k, w.x1 * k, w.bottom * k) for w in words]
    p = _primitives_from_gray(gray, sc, mapped, has_text_layer=True)
    if len(p.orth) + len(p.boxes) < 6:
        return "text"
    W, H = gray.shape[1] / sc, gray.shape[0] / sc
    lens = [(s[2] - s[0], s[3] - s[1]) for s in p.orth]
    total = sum(max(d) for d in lens) or 1.0
    ruled = sum(dx for dx, dy in lens if dx >= dy and dx >= 0.5 * W) + sum(dy for dx, dy in lens if dy > dx and dy >= 0.5 * H)
    long_share = sum(1 for d in lens if max(d) >= REGION_LONG_WIRE_PT) / max(1, len(lens))
    cover = sum((b[2] - b[0]) * (b[3] - b[1]) for b in p.boxes if (b[2] - b[0]) * (b[3] - b[1]) < 0.5 * W * H) / (W * H)
    dots = p.drawn_dots
    if (
        dots >= REGION_DRAWING_MIN_DOTS
        and dots <= REGION_DRAWING_MAX_DOTS_PER_WIRE * len(lens)
        and long_share >= REGION_DRAWING_MIN_LONG_SHARE
        and ruled / total < REGION_RULED_MAX
        and cover < REGION_TABLE_BOX_COVER
    ):
        return "drawing"
    return "table"


def scan_image_region(page, bbox: tuple[float, float, float, float]):
    """OCR one embedded image region of a vector page. Returns (words, kind, prims): the words in
    page coordinates, kind "drawing" | "table" | "text" (_region_kind), and for drawings (a pasted
    schematic scan) the region's raster primitives shifted into page coordinates so its graphics
    are not lost; None otherwise."""
    _require_image_deps()
    import numpy as np

    x0, top, x1, bottom = bbox
    dpi = _region_dpi(page, bbox)
    scale = dpi / 72.0
    gray = np.array(page.crop(bbox).to_image(resolution=dpi).original.convert("L"))
    local = _ocr_words(gray, scale)
    words = [Word(w.text, w.x0 + x0, w.top + top, w.x1 + x0, w.bottom + top) for w in local]
    kind = _region_kind(page, bbox, local)
    if kind != "drawing":
        return words, kind, None
    prims = _primitives_from_gray(gray, scale, [], has_text_layer=True)
    _shift_primitives(prims, x0, top)
    return words, "drawing", prims


def _shift_primitives(prims: Primitives, dx: float, dy: float) -> None:
    """Move region-local primitives into page coordinates."""
    for name in ("orth", "diag", "curves", "boxes", "tris", "strokes", "bars", "rings"):
        setattr(prims, name, [(x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in getattr(prims, name)])
    prims.dots = [(x + dx, y + dy, r) for x, y, r in prims.dots]
