"""Circuit model on top of the raw netlist: named nets, typed parts, values and sanity warnings.

`netlist.extract_design` delivers geometry-level facts (wires, terminals, symbol clusters with
reference designators, net labels). This module turns them into what a netlist consumer needs:

* **Parts** -- one per reference designator, merged across pages. The *kind* (resistor, IC,
  relay contact ...) comes from the refdes prefix and is confirmed or overridden by the drawn
  shape (a slanted blade = switch contact, arcs only = coil). The *value* is the nearest free
  value-looking text (10k, 100nF, 4k7) or, for other parts, the nearest free part-number text.
* **Nets** -- one per connected set of terminals. Named after their net label (the most used
  one when a net carries several aliases), otherwise ``N<page>_<n>``. Power/ground rails are
  recognised by name (GND, VCC, +5V, 3V3 ...) and, unnamed, by being far bigger than any
  signal net of the same document.
* **Warnings** -- dangling single-pin nets, connector flags without a name, pins without a
  number, relay contacts without a coil, nets with conflicting labels.

Everything here is generic schematic knowledge; nothing depends on a particular board family.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .netlist import PINNUM_RE, REFDES_RE, VALUE_RE, Component, Design, Grid, Word, _bbox_gap

REF_PARTS = re.compile(r"^([A-Z]+)(\d+)(?:[_-](\d+))?([A-Z]{1,4})?$")
# 10k, 4k7, 100n, 100nF, 2.2uF, 0R, 1R0, 10m, 1%, 1/4W, 100 ...
VALUE_WORD_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?\s*[pnuµmkKMGR]?(?:F|H|Ω|ohm|OHM|R|V|W|A|Hz|HZ|MHz|MHZ|kHz|KHZ)?\d*"
    r"|\d+[pnuµmkKMR]\d+[A-Za-z]{0,2}|\d+/\d+W|\d+(?:\.\d+)?%)$"
)
# ratings and package codes sit next to the value and must not be taken for it
RATING_RE = re.compile(r"^(?:\d+(?:\.\d+)?[kKmM]?[VWA]|\d+/\d+W|\d+(?:\.\d+)?%|0201|0402|0603|0805|1206|1210|2010|2512)$")
PART_NUMBER_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9.\-/+]{3,}$")  # letters+digits, no '_'
RAIL_RE = re.compile(
    r"GND|VSS|VEE|CHASSIS|EARTH|^0V$|^VCC|^VDD|^VBAT|^VREF|^V[+-]$|"
    r"^[+-]?\d+(?:\.\d+)?V[A-Z0-9_]*$|^\d+V\d+[A-Z0-9_]*$|^[+-]\d+[A-Z0-9_]*V",
    re.I,
)
GROUND_NAMES = {"GND", "0", "0V"}
RAIL_MIN_TERMS = 50  # a net with at least this many pins is a rail even without a name ...
RAIL_P99_FACTOR = 5  # ... when it is also this many times bigger than the document's 99th-percentile net
VALUE_GAP = 12.0  # max gap between a part body and its value / part-number text (pt at reference scale)

# refdes prefix -> kind. Longer prefixes first so "RN" wins over "R".
_PREFIX_KINDS: list[tuple[str, set[str]]] = [
    ("resistor network", {"RN", "RA", "RP"}),
    ("thermistor/varistor", {"RT", "RV", "TH", "NTC", "PTC", "VR", "VDR"}),
    ("ferrite bead", {"FB", "FL", "BLM", "EMI"}),
    ("diode", {"D", "LED", "Z", "ZD", "CR", "VD", "DZ"}),
    ("transistor (BJT)", {"Q"}),
    ("MOSFET", {"M"}),
    ("IC", {"U", "IC", "A", "N", "AR", "OP"}),
    ("relay", {"K", "RLY", "RL", "RY"}),
    ("connector", {"J", "P", "CN", "CON", "X", "XS", "XP", "PL", "SK", "H", "HDR"}),
    ("jumper", {"JP", "JMP", "W", "LK"}),
    ("switch", {"SW", "S"}),
    ("fuse", {"F", "FU"}),
    ("test point", {"TP"}),
    ("fiducial", {"FID", "FD"}),
    ("mechanical", {"MH", "MTG", "MP"}),
    ("crystal", {"Y", "XTAL", "XT"}),
    ("transformer", {"T", "TR"}),
    ("battery", {"BT", "B"}),
    ("source", {"V", "I"}),
    ("resistor", {"R"}),
    ("capacitor", {"C"}),
    ("inductor", {"L"}),
]
_KIND_OF_PREFIX = {p: k for k, ps in _PREFIX_KINDS for p in ps}
# an unknown multi-letter prefix (CP, CTC, RTC ...) falls back to its first letter
_KIND_OF_LETTER = {"R": "resistor", "C": "capacitor", "L": "inductor", "D": "diode", "Q": "transistor (BJT)",
                   "U": "IC", "K": "relay", "J": "connector", "F": "fuse", "Y": "crystal"}
TWO_PIN_KINDS = {"resistor", "capacitor", "inductor", "ferrite bead", "fuse", "jumper", "diode", "relay coil", "source", "battery"}
VALUE_KINDS = TWO_PIN_KINDS | {"resistor network", "thermistor/varistor", "crystal", "switch"}


# --------------------------------------------------------------------------- model
@dataclass
class Pin:
    number: str  # "" when the drawing shows none
    name: str  # pin name inside a box symbol, "" otherwise
    net: str  # net name
    page: int


@dataclass
class Part:
    refdes: str
    prefix: str
    kind: str
    value: str = ""  # component value or part number as printed
    pins: list[Pin] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    is_box: bool = False
    shape: str = ""  # drawn body: e.g. "D1 C2" (1 diagonal, 2 curves)
    notes: list[str] = field(default_factory=list)
    group: str = ""  # parts of one physical device (relay K5_1A / K5_1C) share this key

    @property
    def pin_count(self) -> int:
        return len(self.pins)


@dataclass
class Net:
    name: str
    aliases: list[str] = field(default_factory=list)  # every label text seen on it (incl. name)
    is_rail: bool = False
    rail_reason: str = ""  # "name" | "size"
    pins: list[tuple[str, str, str]] = field(default_factory=list)  # (refdes, pin number, pin name)
    pages: list[int] = field(default_factory=list)
    flags: int = 0  # connector flags without a name attached to this net

    @property
    def is_ground(self) -> bool:
        return self.name.upper() in GROUND_NAMES


@dataclass
class Warning_:
    level: str  # "warn" | "info"
    page: Optional[int]
    text: str


@dataclass
class Circuit:
    source: str
    pages: list[int]
    page_source: dict[int, str]  # page -> "vector" | "vector+ocr" | "raster"
    parts: list[Part]
    nets: list[Net]
    warnings: list[Warning_]
    log: list[str]

    @property
    def rails(self) -> list[Net]:
        return [n for n in self.nets if n.is_rail]

    def kinds(self) -> Counter:
        return Counter(p.kind for p in self.parts)


# --------------------------------------------------------------------------- classification
def refdes_parts(refdes: str) -> tuple[str, int, Optional[str], Optional[str]]:
    m = REF_PARTS.match(refdes)
    if not m:
        return re.sub(r"[^A-Z].*$", "", refdes) or refdes, -1, None, None
    return m.group(1), int(m.group(2)), m.group(3), m.group(4)


def classify(c: Component) -> tuple[str, str]:
    """(kind, prefix) of a symbol cluster from its first refdes and its drawn shape."""
    refdes = c.refdes[0] if c.refdes else ""
    prefix = refdes_parts(refdes)[0] if refdes else ""
    kind = _KIND_OF_PREFIX.get(prefix)
    if not REF_PARTS.match(refdes) and re.search(r"(?:^|_)TP(?:\d|_|$)", refdes):
        kind = "test point"  # free-form names such as TP_REF_1 (the prefix letter means nothing)
    if kind is None and len(prefix) > 2:
        # an unknown long prefix starting with a known multi-letter one (THERM -> TH)
        known = [p for p in _KIND_OF_PREFIX if len(p) > 1 and prefix.startswith(p)]
        if known:
            kind = _KIND_OF_PREFIX[max(known, key=len)]
    if kind is None:
        kind = _KIND_OF_LETTER.get(prefix[:1], "unknown")
    n = len(c.terminals)
    blade = not c.is_box and 1 <= c.shape.get("D", 0) <= 2  # one slanted stroke: a contact, not a zigzag resistor
    if kind == "relay":
        if c.is_zigzag:
            return "resistor", prefix  # RL / RY also name load resistors; the drawn zigzag decides
        if c.is_diode:
            return "relay coil", prefix  # LED of a solid-state relay: its control side
        if blade and n >= 2:
            return "relay contact", prefix
        if c.is_coil or (not c.is_box and n == 2):
            return "relay coil", prefix
        return "relay", prefix
    if kind in ("unknown", "connector") and blade and n >= 2:
        return "switch", prefix
    if kind == "unknown" and c.is_coil:
        return "inductor", prefix
    if kind == "unknown" and c.is_zigzag:
        return "resistor", prefix
    if kind == "unknown" and c.is_diode:
        return "diode", prefix
    if kind == "unknown" and c.is_box and n >= 3:
        return "block", prefix  # hierarchical block / sheet symbol
    if kind in ("connector", "IC") and not c.is_box and n == 2 and prefix in ("P", "X"):
        return "unknown", prefix  # a 2-pin P/X drawn as a plain symbol is rarely a connector
    return kind, prefix


def _shape_text(c: Component) -> str:
    return " ".join(f"{k}{v}" for k, v in sorted(c.shape.items()))


def _pin_sort_key(p: Pin) -> tuple:
    m = re.match(r"^([A-Z]*)(\d+)([A-Z]*)$", p.number)
    return (0, m.group(1), int(m.group(2)), m.group(3)) if m else (1, p.number, 0, p.name)


# --------------------------------------------------------------------------- building
def _net_names(d: Design) -> tuple[dict[tuple, str], dict[tuple, list[str]]]:
    """Global net -> (name, aliases). The name is the most used label, ties to the shortest;
    rail-like labels win so a rail keeps its rail name even with a local alias."""
    counts: dict[tuple, Counter] = defaultdict(Counter)
    for pi, pn in enumerate(d.pages):
        for text, net, _w in pn.labels:
            counts[d.gnet(pi, net)][text] += 1
    names: dict[tuple, str] = {}
    aliases: dict[tuple, list[str]] = {}
    for g, cnt in counts.items():
        ranked = sorted(cnt.items(), key=lambda kv: (0 if RAIL_RE.search(kv[0]) else 1, -kv[1], len(kv[0]), kv[0]))
        names[g] = ranked[0][0]
        aliases[g] = [t for t, _ in ranked]
    return names, aliases


def _assign_values(d: Design, part_of: dict[tuple[int, int], Part]) -> None:
    """Value (passives) or part number (everything else) per symbol: the nearest free text to the
    part's refdes word -- values are printed as a block with the refdes -- else to its body.
    Ratings (50V, 1/4W, 1%) and package codes (0603) rank behind a real value."""
    for pi, pn in enumerate(d.pages):
        S = pn.scale
        gap_max = VALUE_GAP * S
        free = [k for k, w in enumerate(pn.words) if not w.role and re.search(r"[A-Za-z0-9]", w.text) and not REFDES_RE.match(w.text)]
        if not free:
            continue
        grid = Grid(10.0)
        for k in free:
            w = pn.words[k]
            grid.add(k, w.x0, w.top, w.x1, w.bottom)
        ref_words: dict[str, Word] = {}
        for w in pn.words:
            if w.role == "refdes":
                ref_words.setdefault(w.text, w)
        pairs: list[tuple[float, int, int]] = []
        for ci, c in enumerate(pn.components):
            part = part_of.get((pi, ci))
            if part is None or part.value:
                continue
            passive = part.kind in VALUE_KINDS
            anchor = next((ref_words[r] for r in c.refdes if r in ref_words), None)
            ab = anchor.bbox if anchor is not None else c.bbox
            reach = gap_max if passive else 6.0 * S
            for k in grid.query(ab[0] - reach, ab[1] - reach, ab[2] + reach, ab[3] + reach):
                w = pn.words[k]
                if w.role:
                    continue
                is_value = bool(VALUE_WORD_RE.match(w.text) or VALUE_RE.match(w.text))
                if passive and not is_value:
                    continue
                if not passive and (is_value or PINNUM_RE.match(w.text) or not PART_NUMBER_RE.match(w.text)):
                    continue
                if c.is_box and c.bbox[0] <= w.cx <= c.bbox[2] and c.bbox[1] <= w.cy <= c.bbox[3]:
                    continue  # text inside a box is a pin name of an unconnected pin, not the part number
                dist = min(_bbox_gap(w.bbox, ab), _bbox_gap(w.bbox, c.bbox) + 2 * S)
                if dist > reach:
                    continue
                if RATING_RE.match(w.text) or w.text.upper() in ("NOB", "DNP", "DNF"):
                    dist += 100.0
                pairs.append((dist, ci, k))
        pairs.sort()
        for _dist, ci, k in pairs:
            part = part_of[(pi, ci)]
            w = pn.words[k]
            if part.value or w.role:
                continue
            part.value = w.text
            w.role = "value"


def build_circuit(d: Design, source: str = "") -> Circuit:
    """Parts, nets and warnings of an extracted design."""
    log: list[str] = []
    warnings: list[Warning_] = []
    names, aliases = _net_names(d)

    # rails: by name, then by size relative to the document
    sizes = sorted(len(ts) for ts in d.net_terms.values())
    p99 = sizes[int(0.99 * (len(sizes) - 1))] if sizes else 0
    thr = max(RAIL_MIN_TERMS, RAIL_P99_FACTOR * p99)
    rail_reason: dict[tuple, str] = {}
    for g, labs in d.net_labels.items():
        if any(RAIL_RE.search(l) for l in labs):
            rail_reason[g] = "name"
    for g, ts in d.net_terms.items():
        if len(ts) > thr and g not in rail_reason:
            rail_reason[g] = "size"
    by_size = [names.get(g, f"<unnamed, {len(d.net_terms[g])} pins>") for g, r in rail_reason.items() if r == "size"]
    if by_size:
        log.append(
            f"{len(by_size)} net(s) with more than {thr} pins (document p99 = {p99}) treated as power/ground "
            f"rails although their names do not look like one: {by_size[:8]}"
        )

    # unnamed nets: N<page>_<n>, numbered in page order
    unnamed_count: Counter = Counter()
    for pi, pn in enumerate(d.pages):
        seen: set[tuple] = set()
        for t in pn.terminals:
            g = d.gnet(pi, t.net)
            if g in names or g in seen:
                continue
            seen.add(g)
            unnamed_count[pn.page] += 1
            names[g] = f"N{pn.page}_{unnamed_count[pn.page]}"
            aliases[g] = []

    # parts, merged across pages by refdes
    parts: dict[str, Part] = {}
    part_of: dict[tuple[int, int], Part] = {}
    for pi, ci, c in d.comps():
        kind, prefix = classify(c)
        part = parts.get(c.name)
        if part is None:
            part = parts[c.name] = Part(c.name, prefix, kind, is_box=c.is_box, shape=_shape_text(c))
            if c.refdes:
                pr, num, inst, _unit = refdes_parts(c.refdes[0])
                part.group = f"{pr}{num}" + (f"_{inst}" if inst else "")
            if kind == "relay coil" and c.is_diode:
                part.notes.append("drawn as a LED: control input of a solid-state relay")
        elif part.kind != kind and kind != "unknown":
            part.notes.append(f"drawn as {kind} on page {d.pages[pi].page}")
        part_of[(pi, ci)] = part
        pn = d.pages[pi]
        if pn.page not in part.pages:
            part.pages.append(pn.page)
        for ti in c.terminals:
            t = pn.terminals[ti]
            part.pins.append(Pin(t.pin or "", t.pin_name or "", names[d.gnet(pi, t.net)], pn.page))
    _assign_values(d, part_of)

    # a relay drawn in pieces: the boxed unit next to blade contacts is the coil (a coil in a
    # rectangle, often with extra shield / unused pins), not a whole relay
    groups: dict[str, list[Part]] = defaultdict(list)
    for part in parts.values():
        if part.kind.startswith("relay"):
            groups[part.group].append(part)
    for members in groups.values():
        kinds = {m.kind for m in members}
        if "relay contact" in kinds and "relay coil" not in kinds:
            boxed = [m for m in members if m.kind == "relay" and m.is_box]
            if len(boxed) == 1:
                boxed[0].kind = "relay coil"
                boxed[0].notes.append(f"coil drawn as a box with {boxed[0].pin_count} pins")

    # nets
    nets: dict[tuple, Net] = {}
    for g, name in names.items():
        nets[g] = Net(name, aliases.get(g, []), g in rail_reason, rail_reason.get(g, ""))
    for pi, pn in enumerate(d.pages):
        for t in pn.terminals:
            g = d.gnet(pi, t.net)
            net = nets[g]
            if pn.page not in net.pages:
                net.pages.append(pn.page)
            if t.comp is not None:
                net.pins.append((pn.components[t.comp].name, t.pin or "", t.pin_name or ""))
            elif t.label is None:
                net.flags += 1

    # ---- warnings
    for pi, pn in enumerate(d.pages):
        unnamed_flags = sum(1 for t in pn.terminals if t.comp is None and t.label is None)
        if unnamed_flags:
            warnings.append(Warning_("warn", pn.page, f"{unnamed_flags} wire end(s) on a symbol that has neither a reference designator nor a net name (dropped)"))
    dangling = [n for n in nets.values() if len(n.pins) == 1 and not n.aliases and n.flags == 0]
    for n in dangling:
        r, p, _ = n.pins[0]
        warnings.append(Warning_("info", n.pages[0], f"{r} pin {p or '?'} is connected to nothing ({n.name})"))
    for n in nets.values():
        if len(n.aliases) > 1:
            warnings.append(Warning_("info", n.pages[0], f"net {n.name} also carries the label(s) {n.aliases[1:]}"))
    for part in parts.values():
        missing = sum(1 for p in part.pins if not p.number)
        if part.is_box and missing:
            # a symbol with no number printed at all (IEC resistor rectangle, test point, block) is the drawing's style
            level = "info" if missing == len(part.pins) else "warn"
            warnings.append(Warning_(level, part.pages[0], f"{part.refdes}: {missing} of {len(part.pins)} pins have no pin number"))
        dup = [k for k, v in Counter(p.number for p in part.pins if p.number).items() if v > 1]
        if dup:
            nets_of = {num: {p.net for p in part.pins if p.number == num} for num in dup}
            clash = {k: v for k, v in nets_of.items() if len(v) > 1}
            if clash:
                warnings.append(Warning_("warn", part.pages[0], f"{part.refdes}: pin(s) {sorted(clash)} appear on different nets: {clash}"))
        if part.kind in TWO_PIN_KINDS and part.pin_count != 2 and not (part.kind == "relay coil" and part.is_box):
            warnings.append(Warning_("info", part.pages[0], f"{part.refdes} ({part.kind}) has {part.pin_count} pins"))
    groups = defaultdict(list)
    for part in parts.values():
        if part.kind.startswith("relay"):
            groups[part.group].append(part)
    for key, members in groups.items():
        kinds = {m.kind for m in members}
        if "relay contact" in kinds and "relay coil" not in kinds:
            warnings.append(Warning_("warn", members[0].pages[0], f"relay {key}: contact(s) found but no coil"))
        if "relay coil" in kinds and "relay contact" not in kinds:
            warnings.append(Warning_("warn", members[0].pages[0], f"relay {key}: coil found but no contact"))
    no_value = [p.refdes for p in parts.values() if p.kind in TWO_PIN_KINDS and not p.value]
    if no_value:
        warnings.append(Warning_("info", None, f"{len(no_value)} two-pin part(s) without a value text nearby: {no_value[:12]}"))

    circuit = Circuit(
        source, [pn.page for pn in d.pages], {pn.page: pn.source for pn in d.pages},
        sorted(parts.values(), key=lambda p: (p.prefix, refdes_parts(p.refdes)[1], p.refdes)),
        sorted((n for n in nets.values() if n.pins or n.flags), key=lambda n: (not n.is_rail, n.name)), warnings, log,
    )
    for part in circuit.parts:
        part.pins.sort(key=_pin_sort_key)
    kinds = circuit.kinds()
    src = Counter(circuit.page_source.values())
    sizes = sorted({round(pn.text_size, 2) for pn in d.pages if pn.text_size})
    ts = f"{sizes[0]:g}" if len(sizes) == 1 else (f"{sizes[0]:g}-{sizes[-1]:g}" if sizes else "none")
    log.insert(0, f"{len(circuit.pages)} page(s) read ({dict(src)}), drawing text height {ts} pt (tolerance unit)")
    log.insert(1, f"{len(circuit.parts)} parts: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    log.insert(2, f"{len(circuit.nets)} nets with pins, {len(circuit.rails)} power/ground rails: "
                  + ", ".join(n.name for n in circuit.rails[:12]))
    log.append(f"{sum(1 for w in warnings if w.level == 'warn')} warning(s), {sum(1 for w in warnings if w.level == 'info')} note(s)")
    return circuit
