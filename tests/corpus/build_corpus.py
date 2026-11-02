"""Build the ground-truth corpus from the KiCad demo projects: PDF + kicadxml netlist per project,
plus black&white and uniformly rescaled PDF variants (the netlist must not depend on drawing size)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

KICAD = max((Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad").glob("*"), default=Path(r"C:\Program Files\KiCad\10.0"))
CLI = KICAD / "bin" / "kicad-cli.exe"
DEMOS = KICAD / "share" / "kicad" / "demos"
OUT = Path(__file__).parent / "cases"
SCALES = (0.4, 3.0)


def run(*args) -> bool:
    r = subprocess.run([str(CLI), *map(str, args)], capture_output=True, text=True)
    if r.returncode:
        print("   ", r.stderr.strip()[:300] or r.stdout.strip()[:300])
    return r.returncode == 0


def rescale(src: Path, dst: Path, f: float) -> None:
    """Uniformly scale every page (geometry, text, line widths) by f."""
    s = pdfium.PdfDocument(str(src))
    d = pdfium.PdfDocument.new()
    for i in range(len(s)):
        w, h = s[i].get_size()
        xo = s.page_as_xobject(i, d)
        po = xo.as_pageobject()
        po.transform(pdfium.PdfMatrix().scale(f, f))
        page = d.new_page(w * f, h * f)
        page.insert_obj(po)
        page.gen_content()
    d.save(str(dst))
    s.close()
    d.close()


def main() -> None:
    only = sys.argv[1:]
    OUT.mkdir(exist_ok=True)
    for pro in sorted(DEMOS.rglob("*.kicad_pro")):
        sch = pro.with_suffix(".kicad_sch")
        if not sch.exists():
            continue
        name = pro.stem.replace(" ", "_")
        if only and name not in only:
            continue
        print(name, flush=True)
        case = OUT / name
        case.mkdir(exist_ok=True)
        pdf, xml = case / f"{name}.pdf", case / f"{name}.xml"
        if not (run("sch", "export", "pdf", "-o", pdf, sch) and run("sch", "export", "netlist", "--format", "kicadxml", "-o", xml, sch)):
            shutil.rmtree(case, ignore_errors=True)
            continue
        bw = OUT / f"{name}__bw"
        bw.mkdir(exist_ok=True)
        run("sch", "export", "pdf", "--black-and-white", "-o", bw / f"{name}__bw.pdf", sch)
        shutil.copy(xml, bw / f"{name}__bw.xml")
        for f in SCALES:
            v = OUT / f"{name}__x{f:g}"
            v.mkdir(exist_ok=True)
            rescale(pdf, v / f"{v.name}.pdf", f)
            shutil.copy(xml, v / f"{v.name}.xml")


if __name__ == "__main__":
    main()
