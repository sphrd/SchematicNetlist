"""CLI: `schematic-netlist analyze board.pdf` -> analysis workbook, HSPICE deck, flat netlist, log."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import OUTPUTS, analyze_pdf
from .doctor import format_report, run_checks


def parse_page_range(spec: str) -> list[int]:
    """'1-3,7' -> [1, 2, 3, 7]."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = run_checks()
    print(format_report(checks))
    return 1 if any(c.status == "missing" for c in checks) else 0


def cmd_analyze(args: argparse.Namespace) -> int:
    pdf = Path(args.pdf)
    name = args.name or pdf.stem
    out_dir = Path(args.output) if args.output else pdf.parent / pdf.stem
    outputs = [o for o in OUTPUTS if not getattr(args, f"no_{o}")]

    def progress(i: int, n: int) -> None:
        if i == n or i % 10 == 0:
            print(f"  page {i}/{n}", file=sys.stderr)

    res = analyze_pdf(
        pdf, name, out_dir, outputs, pages=parse_page_range(args.pages) if args.pages else None,
        progress=progress, workers=args.workers, use_cache=not args.no_cache,
    )
    for line in res.circuit.log:
        print(line)
    for f in res.files:
        print(f"-> {f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schematic-netlist",
        description="Read a schematic PDF (vector drawing or scan) and write its component list, nets, "
        "an HSPICE netlist and an Excel analysis report.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="Check that required/optional dependencies (incl. Tesseract) are ready.")
    p_doctor.set_defaults(func=cmd_doctor)

    p = sub.add_parser("analyze", help="Schematic PDF -> <name>_analysis.xlsx, <name>.sp, <name>_netlist.txt/.json, engine log.")
    p.add_argument("pdf", help="Path to the schematic PDF.")
    p.add_argument("-o", "--output", help="Output folder. Default: a folder named after the PDF, next to it.")
    p.add_argument("--name", help="Project name used for the output files and the SPICE title. Default: PDF file name.")
    p.add_argument("--pages", help="Page range, e.g. '1-3,5'. Default: all pages.")
    p.add_argument("--no-excel", action="store_true", help="Skip the Excel analysis report.")
    p.add_argument("--no-spice", action="store_true", help="Skip the HSPICE netlist.")
    p.add_argument("--no-netlist", action="store_true", help="Skip the flat text/JSON netlist.")
    p.add_argument("--workers", type=int, help="Parallel page readers. Default: one per CPU core (max 8); 1 = sequential.")
    p.add_argument("--no-cache", action="store_true", help="Re-read the PDF even if a cached netlist of it exists.")
    p.set_defaults(func=cmd_analyze)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
