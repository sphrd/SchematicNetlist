"""HSPICE netlist from a `Circuit`.

What a schematic drawing can tell is connectivity, part kinds and printed values -- not
models. The deck is therefore written to be *editable*: every unknown value is a ``.PARAM``
with a commented default, every semiconductor gets a stub ``.MODEL``, every IC / multi-pin
part an empty ``.SUBCKT`` whose pin order is documented, and relays become a voltage-controlled
switch driven by their own coil. Connectors and test points are listed as comments.

Element mapping (by part kind, see `circuit.classify`):

    resistor / fuse / jumper      R         capacitor      C        inductor / ferrite   L
    diode                         D + .MODEL  BJT          Q + .MODEL  MOSFET            M + .MODEL
    relay coil                    R (coil resistance, so a coil voltage exists in DC)
    relay contact                 S switch controlled by the coil nodes of the same relay
    switch                        R with a parameter (1m closed / 1G open)
    source / battery              V
    IC, networks, crystals, transformers, anything with an unexpected pin count
                                  X instance of a stub .SUBCKT
    connector / test point        comment lines
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

from .circuit import Circuit, Part, Pin

_DEFAULTS = {  # kind -> (parameter suffix, default value)
    "resistor": ("r", "1k"),
    "capacitor": ("c", "100n"),
    "inductor": ("l", "1u"),
    "ferrite bead": ("l", "1u"),
    "fuse": ("r", "10m"),
    "jumper": ("r", "1m"),
    "switch": ("r", "1m"),
    "relay coil": ("r", "500"),
    "source": ("v", "5"),
    "battery": ("v", "3"),
}
_UNIT_FIX = [  # printed value -> SPICE number
    (re.compile(r"(?i)\s*(ohm|Ω)s?$"), ""),
    (re.compile(r"(?i)^(\d+)R(\d+)$"), r"\1.\2"),  # 4R7 -> 4.7
    (re.compile(r"(?i)^(\d+)R$"), r"\1"),  # 0R -> 0
    (re.compile(r"(?i)^(\d+)([kKmMuµnpG])(\d+)"), r"\1.\3\2"),  # 4k7 -> 4.7k
    (re.compile(r"µ"), "u"),
    (re.compile(r"(?i)^(\d+(?:\.\d+)?)M(?![eE])$"), r"\1meg"),  # 1M ohm/farad -> 1meg (SPICE 'm' is milli)
    (re.compile(r"(?i)(F|H|V|W|A|Hz)$"), ""),
]
_NUMBER = re.compile(r"^\d+(?:\.\d+)?(?:e[+-]?\d+)?(?:meg|[tgkmunpf])?$", re.I)


def spice_value(text: str) -> str | None:
    """'4k7' -> '4.7k', '100nF' -> '100n', '1M' -> '1meg'; None if it is not a number."""
    v = text.strip()
    for rx, rep in _UNIT_FIX:
        v = rx.sub(rep, v)
    return v if _NUMBER.match(v) else None


def _ident(text: str) -> str:
    """A name HSPICE accepts for nodes, instances, models and parameters."""
    t = text.strip()
    t = re.sub(r"^\+", "P", t)
    t = re.sub(r"^-", "N", t)
    t = re.sub(r"^/", "n_", t)  # /RESET -> n_RESET (active-low)
    t = re.sub(r"[^A-Za-z0-9_]", "_", t)
    return t or "_"


class _Names:
    """Unique sanitized names: two different nets that sanitize equally get _2, _3 ..."""

    def __init__(self) -> None:
        self.used: dict[str, str] = {}
        self.map: dict[str, str] = {}

    def __call__(self, raw: str) -> str:
        if raw in self.map:
            return self.map[raw]
        base = _ident(raw)
        name, n = base, 1
        while name.lower() in self.used and self.used[name.lower()] != raw:
            n += 1
            name = f"{base}_{n}"
        self.used[name.lower()] = raw
        self.map[raw] = name
        return name


def _instance(letter: str, refdes: str) -> str:
    r = _ident(refdes)
    return r if r.upper().startswith(letter) else letter + r


def _pins_in_order(part: Part) -> list[Pin]:
    """Pins by number; merged duplicates (same pin on several pages) count once."""
    seen: dict[str, Pin] = {}
    out: list[Pin] = []
    for p in part.pins:
        key = p.number or f"{p.name}@{p.page}@{id(p)}"
        if key in seen:
            continue
        seen[key] = p
        out.append(p)
    return out


def write_spice(circuit: Circuit, title: str) -> str:
    node = _Names()
    for n in circuit.nets:
        if n.is_ground:
            node.map[n.name] = "0"
    lines: list[str] = []
    params: list[tuple[str, str, str]] = []  # (name, default, comment)
    models: dict[str, str] = {}
    subckts: list[list[str]] = []
    comments: list[str] = []
    coil_nodes: dict[str, tuple[str, str]] = {}
    parts_by_group: dict[str, list[Part]] = defaultdict(list)
    for part in circuit.parts:
        parts_by_group[part.group].append(part)
        if part.kind == "relay coil" and part.pin_count >= 2:
            a, b = (node(p.net) for p in _pins_in_order(part)[:2])
            coil_nodes[part.group] = (a, b)

    def val(part: Part) -> str:
        suffix, default = _DEFAULTS.get(part.kind, ("val", "1"))
        v = spice_value(part.value) if part.value else None
        if v is not None and part.kind not in ("relay coil", "switch"):
            return v
        name = f"{_ident(part.refdes)}_{suffix}"
        why = f"printed value {part.value!r} is not a number" if part.value else "no value in the drawing"
        if part.kind == "relay coil":
            why = f"coil resistance ({part.value or 'no value printed'})"
        if part.kind == "switch":
            why = "1m = closed, 1G = open"
        params.append((name, default, why))
        return f"'{name}'"

    def nodes(part: Part, count: int | None = None) -> list[str]:
        ps = _pins_in_order(part)
        out = [node(p.net) for p in ps]
        if count is not None:
            out = out[:count]
        return out

    def pin_doc(part: Part) -> str:
        return " ".join(f"{p.number or '?'}" + (f"={p.name}" if p.name else "") for p in _pins_in_order(part))

    def subckt(part: Part) -> None:
        name = f"SUB_{_ident(part.refdes)}"
        ps = _pins_in_order(part)
        ports = [_ident(p.number or p.name or f"p{i}") for i, p in enumerate(ps, start=1)]
        used: set[str] = set()
        for i, p in enumerate(ports):
            while p in used:
                p += "_"
            ports[i] = p
            used.add(p)
        body = [f".SUBCKT {name} {' '.join(ports)}", f"* {part.refdes}: {part.kind}" + (f", {part.value}" if part.value else "")]
        if part.kind.startswith("relay"):
            ctl = coil_nodes.get(part.group)
            body.append(f"* relay {part.group} coil nodes: {ctl[0]} {ctl[1]}" if ctl else f"* relay {part.group}: no coil drawn")
        for p, port in zip(ps, ports):
            body.append(f"* pin {port}" + (f" = {p.name}" if p.name else ""))
        body.append("* (empty stub: add the model or a behavioural description here)")
        body.append(".ENDS")
        subckts.append(body)
        lines.append(f"X{_ident(part.refdes)} {' '.join(nodes(part))} {name}")

    for part in circuit.parts:
        k = part.kind
        n = part.pin_count
        if k in ("connector", "test point", "fiducial", "mechanical"):
            comments.append(f"* {k} {part.refdes}" + (f" ({part.value})" if part.value else "") + ": " + ", ".join(
                f"{p.number or '?'}" + (f"/{p.name}" if p.name else "") + f"={node(p.net)}" for p in _pins_in_order(part)))
            continue
        if n == 0:
            comments.append(f"* {part.refdes} ({k}): no pins found")
            continue
        if k in ("resistor", "fuse", "jumper", "switch", "relay coil") and n == 2:
            a, b = nodes(part, 2)
            tag = "" if k == "resistor" else f"  $ {k}"
            lines.append(f"{_instance('R', part.refdes)} {a} {b} {val(part)}{tag}")
        elif k == "relay coil" and n > 2:
            a, b = nodes(part, 2)
            lines.append(f"{_instance('R', part.refdes)} {a} {b} {val(part)}  $ relay coil on the two lowest pins of {pin_doc(part)}; the others are left open")
        elif k == "capacitor" and n == 2:
            a, b = nodes(part, 2)
            lines.append(f"{_instance('C', part.refdes)} {a} {b} {val(part)}")
        elif k in ("inductor", "ferrite bead") and n == 2:
            a, b = nodes(part, 2)
            lines.append(f"{_instance('L', part.refdes)} {a} {b} {val(part)}" + ("  $ ferrite bead" if k == "ferrite bead" else ""))
        elif k == "diode" and n == 2:
            a, b = nodes(part, 2)
            m = f"D_{_ident(part.value or part.refdes)}"
            models.setdefault(m, f".MODEL {m} D")
            lines.append(f"{_instance('D', part.refdes)} {a} {b} {m}  $ pins {pin_doc(part)}: check anode/cathode")
        elif k == "transistor (BJT)" and n == 3:
            m = f"Q_{_ident(part.value or part.refdes)}"
            models.setdefault(m, f".MODEL {m} NPN")
            lines.append(f"{_instance('Q', part.refdes)} {' '.join(nodes(part, 3))} {m}  $ pins {pin_doc(part)}: check C B E order")
        elif k == "MOSFET" and n in (3, 4):
            m = f"M_{_ident(part.value or part.refdes)}"
            models.setdefault(m, f".MODEL {m} NMOS")
            ns = nodes(part, 4)
            if len(ns) == 3:
                ns.append(ns[2])
            lines.append(f"{_instance('M', part.refdes)} {' '.join(ns)} {m}  $ pins {pin_doc(part)}: check D G S B order")
        elif k in ("source", "battery") and n == 2:
            a, b = nodes(part, 2)
            lines.append(f"{_instance('V', part.refdes)} {a} {b} DC {val(part)}")
        elif k == "relay contact" and n == 2:
            a, b = nodes(part, 2)
            ctl = coil_nodes.get(part.group)
            m = f"SW_{_ident(part.group)}"
            models.setdefault(m, f".MODEL {m} SW VT=2.5 VH=0.5 RON=0.1 ROFF=1G")
            if ctl is None:
                cn = f"{_ident(part.group)}_ctl"
                ctl = (cn, "0")
                coil_nodes[part.group] = ctl
                lines.append(f"V{cn} {cn} 0 DC 0  $ relay {part.group}: no coil drawn; set > VT to close its contacts")
            lines.append(f"{_instance('S', part.refdes)} {a} {b} {ctl[0]} {ctl[1]} {m}  $ relay {part.group} contact")
        else:
            subckt(part)

    rails = [node(n.name) for n in circuit.nets if n.is_rail and not n.is_ground]
    out = [
        f"* {title} -- HSPICE netlist generated by SchematicNetlist from {circuit.source}",
        f"* {datetime.now():%Y-%m-%d %H:%M}; {len(circuit.pages)} page(s), {len(circuit.parts)} parts, {len(circuit.nets)} nets",
        "* Connectivity and printed values come from the drawing; models, subcircuits and the",
        "* parameters below are placeholders to be completed before simulating.",
        f".TITLE {_ident(title)}",
        "",
    ]
    if rails:
        out.append(".GLOBAL " + " ".join(rails))
        out.append("")
    if params:
        out.append("* ---- values the drawing did not give (edit as needed)")
        out += [f".PARAM {n}={d}  $ {why}" for n, d, why in params]
        out.append("")
    out.append("* ---- elements")
    out += lines
    if comments:
        out += ["", "* ---- connectors and test points (pin=node)"] + comments
    if models:
        out += ["", "* ---- stub models"] + list(models.values())
    if subckts:
        out += ["", "* ---- stub subcircuits"]
        for body in subckts:
            out += body + [""]
    out += [".END", ""]
    return "\n".join(out)
