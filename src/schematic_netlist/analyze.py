"""One call from a schematic PDF to all output files (shared by the CLI and the GUI)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .circuit import Circuit, build_circuit
from .netlist import extract_design
from .netlist_export import write_json_netlist, write_text_netlist
from .report_excel import write_report
from .spice import write_spice

OUTPUTS = ("excel", "spice", "netlist")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def bad_file_name(name: str) -> Optional[str]:
    """Why ``name`` can't name the output folder and files on Windows; None when it can."""
    bad = sorted({c for c in name if c in '<>:"/\\|?*' or ord(c) < 32})
    if bad:
        return "it can't contain " + " ".join(bad) + "."
    if name != name.rstrip(" ."):
        return "it can't end with a space or a dot."
    if name.split(".")[0].upper() in _RESERVED:
        return f"{name.split('.')[0]} is reserved by Windows."
    return None


@dataclass
class Result:
    circuit: Circuit
    files: list[Path] = field(default_factory=list)


def analyze_pdf(
    pdf_path: str | Path,
    name: str,
    out_dir: str | Path,
    outputs: Iterable[str] = OUTPUTS,
    pages: Optional[Iterable[int]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    workers: Optional[int] = None,
    activity: Optional[Callable[[Optional[int], str, str], None]] = None,
) -> Result:
    """``activity(page, state, text)``: live view of the engine's work (see extract_design), followed
    by this module's own steps (page None)."""
    pdf_path, out_dir, outputs = Path(pdf_path), Path(out_dir), set(outputs)
    say = activity or (lambda *_a: None)
    design = extract_design(str(pdf_path), pages, progress=progress, cancel_check=cancel_check, workers=workers, activity=activity)
    say(None, "work", "classifying parts and checking the circuit")
    circuit = build_circuit(design, pdf_path.name)
    say(None, "done", f"{len(circuit.parts)} parts, {len(circuit.nets)} nets, {len(circuit.warnings)} warnings")
    out_dir.mkdir(parents=True, exist_ok=True)
    res = Result(circuit)

    def put(file: Path, text: str) -> None:
        file.write_text(text, encoding="utf-8")
        res.files.append(file)
        say(None, "done", f"wrote {file.name}")

    if "excel" in outputs:
        xlsx = out_dir / f"{name}_analysis.xlsx"
        say(None, "work", f"writing {xlsx.name}")
        write_report(xlsx, circuit, name)
        res.files.append(xlsx)
        say(None, "done", f"wrote {xlsx.name}")
    if "spice" in outputs:
        put(out_dir / f"{name}.sp", write_spice(circuit, name))
    if "netlist" in outputs:
        put(out_dir / f"{name}_netlist.txt", write_text_netlist(circuit, name))
        put(out_dir / f"{name}_netlist.json", write_json_netlist(circuit, name))
    log = circuit.log + [""] + [f"[{w.level}] " + (f"page {w.page}: " if w.page else "") + w.text for w in circuit.warnings]
    put(out_dir / f"{name}_engine_log.txt", "\n".join(log) + "\n")
    return res
