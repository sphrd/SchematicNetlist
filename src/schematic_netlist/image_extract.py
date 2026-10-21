"""Raster front-end for schematic pages without vector drawing (scans, flattened exports).

Recovers from pixels the same primitives the vector path reads from the PDF -- orthogonal
wire segments, junction dots, symbol blobs, boxes and words -- so `netlist.build_page`
treats both kinds of page identically. Needs the `image` extra (opencv-python-headless,
pytesseract, numpy); OCR also needs the Tesseract binary unless the page has a text layer.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from .netlist import DOT_MAX, Primitives, Word

_TESSERACT_HINTS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
_OCR_MIN_CONF = 30
_OCR_SHORT_MIN_CONF = 70  # 1-2 character tokens: symbols (chevrons, contacts) are often misread as such
_OCR_ROTATED_MIN_CONF = 60
OCR_GLYPH_PX = 32  # glyph height Tesseract reads best
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


def _require_image_deps() -> None:
    missing = []
    for mod, pkg in (("cv2", "opencv-python-headless"), ("pytesseract", "pytesseract"), ("numpy", "numpy")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise RuntimeError(
            "This page has no vector drawing (scanned/flattened), and reading it needs: "
            + ", ".join(missing)
            + '.\nInstall with:  uv pip install -e ".[image]"  (and the Tesseract engine: '
            "winget install UB-Mannheim.TesseractOCR)"
        )


TESSERACT_ZIP = "tesseract.zip"  # packed into SchematicNetlist.exe by src/build_exe.py


def _bundled_tesseract() -> str | None:
    """SchematicNetlist.exe carries Tesseract + the English model as one zip; it is unpacked the
    first time OCR is needed (not at every start) into %LOCALAPPDATA% and reused afterwards."""
    packed = Path(getattr(sys, "_MEIPASS", "")) / TESSERACT_ZIP
    if not getattr(sys, "frozen", False) or not packed.exists():
        return None
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SchematicNetlist"
    target = base / f"tesseract-{packed.stat().st_size}"
    exe = target / "tesseract.exe"
    if not exe.exists():
        import zipfile

        tmp = target.with_name(target.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(packed) as z:
            z.extractall(tmp)
        tmp.replace(target)
    os.environ["TESSDATA_PREFIX"] = str(target / "tessdata")
    return str(exe)


def find_tesseract() -> str | None:
    bundled = _bundled_tesseract()
    if bundled:
        return bundled
    path = shutil.which("tesseract")
    if path:
        return path
    return next((h for h in _TESSERACT_HINTS if Path(h).exists()), None)


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
    """Words of a rendered page via Tesseract (for vector pages without a text layer)."""
    _require_image_deps()
    import numpy as np

    gray = np.array(page.to_image(resolution=resolution).original.convert("L"))
    return _ocr_words(gray, resolution / 72.0)


# --------------------------------------------------------------------------- words
def _clean_token(text: str) -> str:
    t = _TOKEN_TRIM.sub("", text.strip())
    if len(t) >= 4 and sum(c.isdigit() for c in t) >= 0.75 * len(t):
        return t.translate(_OCR_DIGIT_FIX)
    # O/0 inside codes such as 893-F03OUT1: a zero before two letters is an O, an O next to a digit a zero
    t = re.sub(r"0(?=[A-Z]{2})", "O", t)
    t = re.sub(r"(?<=[A-Z])O(?=\d)|(?<=\d)O(?=\d)|(?<=\d)O\b", "0", t)
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


def _ocr_words(gray, scale: float) -> list[Word]:
    import cv2
    import numpy as np
    import pytesseract

    cmd = find_tesseract()
    if cmd is None:
        raise RuntimeError(
            "This page has no vector drawing and no text layer, so it needs OCR, but the Tesseract "
            "engine was not found. Install it:  winget install UB-Mannheim.TesseractOCR"
        )
    pytesseract.pytesseract.tesseract_cmd = cmd
    config = "--psm 11 --oem 1"  # sparse text; a character whitelist makes the LSTM model worse
    # schematic text is ~3.5 pt; Tesseract wants ~30 px glyphs, so upsample small renders
    up = max(1.0, OCR_GLYPH_PX / (3.5 * scale))
    s = scale * up

    # wires touching text break Tesseract's segmentation: erase long strokes first
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    k = max(3, int(OCR_ERASE_PT * scale))
    long_strokes = cv2.bitwise_or(
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1))),
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k))),
    )
    gray = gray.copy()
    gray[cv2.dilate(long_strokes, np.ones((3, 3), np.uint8)) > 0] = 255
    # light-grey text (a common label colour) reads far better stretched to full contrast
    ink_level = int(np.percentile(gray[ink > 0], 90)) if (ink > 0).any() else 0
    gray = cv2.normalize(np.clip(gray, 0, max(ink_level + 60, 128)), None, 0, 255, cv2.NORM_MINMAX)

    def read(img, to_pdf, min_conf: int) -> list[Word]:
        if up > 1.0:
            img = cv2.resize(img, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
        found = []
        for i, raw in enumerate(data["text"]):
            text = _clean_token(raw or "")
            conf = float(data["conf"][i])
            if text and conf >= (min_conf if len(text) >= 3 else _OCR_SHORT_MIN_CONF):
                found.append(Word(text, *to_pdf(data["left"][i], data["top"][i], data["width"][i], data["height"][i])))
        return found

    words = read(gray, lambda l, t, w, h: (l / s, t / s, (l + w) / s, (t + h) / s), _OCR_MIN_CONF)

    # vertical text (rotated labels, pin numbers of vertical pins): OCR the page turned 90 deg clockwise
    H = gray.shape[0] * up
    rotated = read(
        cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE),
        lambda l, t, w, h: (t / s, (H - (l + w)) / s, (t + h) / s, (H - l) / s),
        _OCR_ROTATED_MIN_CONF,
    )
    for w in rotated:
        if len(w.text) >= 2 and not any(
            w.x0 < v.x1 and v.x0 < w.x1 and w.top < v.bottom and v.top < w.bottom for v in words
        ):
            words.append(w)
    return _join_underscores(words, ink, scale)


def _ocr_rows(gray, rows: list[tuple[int, int, int, int]], scale: float) -> list[Word]:
    """Second OCR pass: each text row the page-level pass missed, cropped and read as one line."""
    import cv2
    import pytesseract

    pad = int(1.0 * scale)
    H, W = gray.shape
    out: list[Word] = []
    for x0, y0, x1, y1 in rows:
        cx0, cy0, cx1, cy1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
        crop = gray[cy0:cy1, cx0:cx1]
        up = max(1.0, OCR_GLYPH_PX / max(y1 - y0, 1))
        crop = cv2.resize(crop, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
        crop = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
        data = pytesseract.image_to_data(crop, config="--psm 7 --oem 1", output_type=pytesseract.Output.DICT)
        for i, raw in enumerate(data["text"]):
            text = _clean_token(raw or "")
            if len(text) >= 3 and float(data["conf"][i]) >= _OCR_MIN_CONF:
                l, t = (data["left"][i] - 10) / up + cx0, (data["top"][i] - 10) / up + cy0
                w, h = data["width"][i] / up, data["height"][i] / up
                out.append(Word(text, l / scale, t / scale, (l + w) / scale, (t + h) / scale))
    return out


def _join_underscores(words: list[Word], ink, scale: float) -> list[Word]:
    """Tesseract drops underscores and splits there (J2_1 -> J2, 1): rejoin neighbours on one
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
    import cv2
    import numpy as np

    resolution = resolution or native_resolution(page)
    scale = resolution / 72.0
    gray = np.array(page.to_image(resolution=resolution).original.convert("L"))
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # text must not become wires or symbols; long strokes (wires running under a label) stay
    words = _text_layer_words(page) or _ocr_words(gray, scale)
    kl = max(3, int(OCR_ERASE_PT * scale))
    long_strokes = cv2.bitwise_or(
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (kl, 1))),
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, kl))),
    )
    text, unread = _text_components(cv2.bitwise_and(ink, cv2.bitwise_not(long_strokes)), words, scale)
    if unread and not page.chars:
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
        orth, [], curves, boxes, dots, words, tol=RASTER_TOL, colinear_tol=0.6, source="raster", dotless_t_min=DOTLESS_T_MIN_PT
    )
