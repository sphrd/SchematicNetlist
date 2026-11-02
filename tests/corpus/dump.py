"""Dump the engine's view of one page: components, terminals, labels, unattached words."""
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[3] if len(sys.argv) > 3 else str(Path(__file__).resolve().parents[2] / "src"))
import pdfplumber  # noqa: E402

from schematic_netlist.netlist import extract_page  # noqa: E402

pdf, pno = sys.argv[1], int(sys.argv[2])
with pdfplumber.open(pdf) as doc:
    pn = extract_page(doc.pages[pno - 1], pno)
print(f"source={pn.source} words={len(pn.words)} segs={len(pn.segs)} terms={len(pn.terminals)} comps={len(pn.components)}")
for i, c in enumerate(pn.components):
    b = tuple(round(v) for v in c.bbox)
    pins = [(pn.terminals[t].pin, pn.terminals[t].net) for t in c.terminals]
    print(f" C{i} {c.refdes} box={c.is_box} bbox={b} shape={c.shape} pins={pins}")
print("labels:", [(t, n) for t, n, _ in pn.labels])
print("roles:", sorted({(w.text, w.role) for w in pn.words if w.role})[:80])
print("free words:", [w.text for w in pn.words if not w.role][:80])
