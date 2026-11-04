"""Score the SchematicNetlist engine against KiCad ground truth.

cases/<case>/<case>.pdf + <case>.xml (kicad-cli sch export netlist --format kicadxml).
Metrics (power symbols / no-pin parts excluded, they are not in the KiCad netlist):
  parts  : recall of GT refdes found as a part
  pins   : recall of GT (ref, pin) found with a net
  conn P/R: pin-pair connectivity precision / recall over pins found in both
Usage: python score.py [case-glob] [--engine path\\to\\src] [-v]
"""
from __future__ import annotations

import argparse
import os
import fnmatch
import itertools
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent


def gt_netlist(xml: Path):
    root = ET.parse(xml).getroot()
    pins: dict[tuple[str, str], str] = {}
    for net in root.iter("net"):
        code = net.get("code")
        for node in net.iter("node"):
            pins[(node.get("ref"), node.get("pin"))] = code
    refs = {r for r, _ in pins}
    return refs, pins


def c2(n: int) -> int:
    return n * (n - 1) // 2


def norm_ref(r: str, gt_refs: set[str]) -> str:
    if r in gt_refs:
        return r
    m = re.match(r"^(.*\d)[A-Z]$", r)  # KiCad multi-unit U1A -> U1
    if m and m.group(1) in gt_refs:
        return m.group(1)
    return r


def score_case(case: Path, engine_src: str) -> dict:
    if engine_src not in sys.path:
        sys.path.insert(0, engine_src)
    from schematic_netlist.circuit import build_circuit
    from schematic_netlist.netlist import extract_design

    pdf = next(case.glob("*.pdf"))
    xml = next(case.glob("*.xml"))
    gt_refs, gt = gt_netlist(xml)
    t = time.time()
    d = extract_design(str(pdf))
    c = build_circuit(d, pdf.name)
    secs = time.time() - t

    found_refs = {norm_ref(p.refdes, gt_refs) for p in c.parts}
    pred: dict[tuple[str, str], str] = {}
    unnumbered: dict[str, list[str]] = defaultdict(list)
    for n in c.nets:
        for r, p, _nm in n.pins:
            r = norm_ref(r, gt_refs)
            if p and (r, p) in gt:
                pred[(r, p)] = n.name
            else:
                unnumbered[r].append(n.name)
    gt_by_ref: dict[str, list[str]] = defaultdict(list)
    for r, p in gt:
        gt_by_ref[r].append(p)
    by_code: dict[str, list] = defaultdict(list)
    for k, code in gt.items():
        by_code[code].append(k)
    # pins whose number was not read (or not printed): best assignment to the part's free GT pins,
    # by coordinate ascent from a few seeded starts (2-pin parts with hidden numbers have no
    # anchor, so a single greedy pass depends on the order the parts come in)
    numbered = set(pred)
    jobs = []
    for r, nets in unnumbered.items():
        free = [p for p in gt_by_ref.get(r, []) if (r, p) not in numbered]
        if free and len(free) <= 5 and len(nets) <= 5:
            jobs.append((r, nets, list(itertools.permutations(free, min(len(free), len(nets))))))

    def tp_of(pr):
        cont = Counter((gt[k], pr[k]) for k in gt if k in pr)
        return sum(c2(v) for v in cont.values())

    import random
    best_pred, best_tp = None, -1
    for seed in range(int(os.environ.get("SCORE_SEEDS", "6"))):
        rnd = random.Random(seed)
        pr = dict(pred)
        order = list(jobs)
        if seed:
            rnd.shuffle(order)
            for r, nets, perms in order:
                for p, n in zip(rnd.choice(perms), nets):
                    pr[(r, p)] = n
        for _round in range(8):
            changed = False
            for r, nets, perms in order:
                old = {p: pr.pop((r, p)) for p in {q for pm in perms for q in pm} if (r, p) in pr}
                best, bs = None, -1
                for perm in perms:
                    s = 0
                    for p, n in zip(perm, nets):
                        s += sum(1 for q in by_code[gt[(r, p)]] if q in pr and pr[q] == n)
                    if s > bs or (s == bs and all(old.get(p) == n for p, n in zip(perm, nets))):
                        best, bs = perm, s
                for p, n in zip(best, nets):
                    pr[(r, p)] = n
                changed |= any(old.get(p) != n for p, n in zip(best, nets))
            if not changed:
                break
        v = tp_of(pr)
        if v > best_tp:
            best_pred, best_tp = pr, v
    pred = best_pred

    common = [k for k in gt if k in pred]
    cont = Counter((gt[k], pred[k]) for k in common)
    tp = sum(c2(v) for v in cont.values())
    gp = sum(c2(v) for v in Counter(gt[k] for k in common).values())
    pp = sum(c2(v) for v in Counter(pred[k] for k in common).values())
    return {
        "case": case.name,
        "parts": len(gt_refs & found_refs) / max(1, len(gt_refs)),
        "pins": len(common) / max(1, len(gt)),
        "connP": tp / pp if pp else 1.0,
        "connR": tp / gp if gp else 1.0,
        "gt_parts": len(gt_refs),
        "extra_parts": len(found_refs - gt_refs),
        "secs": secs,
        "missing_refs": sorted(gt_refs - found_refs)[:15],
        "extra_refs": sorted(found_refs - gt_refs)[:15],
        "_pred": pred,
        "_gt": gt,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default="*")
    ap.add_argument("--engine", default=str(Path(__file__).resolve().parents[2] / "src"))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    cases = sorted(p for p in (HERE / "cases").iterdir() if p.is_dir() and fnmatch.fnmatch(p.name, a.pattern))
    rows = []
    for case in cases:
        try:
            r = score_case(case, a.engine)
        except Exception as e:  # noqa: BLE001
            print(f"{case.name:34s} ERROR {e!r}", flush=True)
            continue
        rows.append(r)
        print(
            f"{r['case']:34s} parts {r['parts']:6.1%} ({r['gt_parts']:3d}, +{r['extra_parts']:2d})  pins {r['pins']:6.1%}  "
            f"connP {r['connP']:6.1%}  connR {r['connR']:6.1%}  {r['secs']:5.1f}s",
            flush=True,
        )
        if a.v:
            print(f"    missing {r['missing_refs']}\n    extra   {r['extra_refs']}")
    if rows:
        avg = lambda k: sum(r[k] for r in rows) / len(rows)  # noqa: E731
        print(f"{'MEAN':34s} parts {avg('parts'):6.1%}             pins {avg('pins'):6.1%}  connP {avg('connP'):6.1%}  connR {avg('connR'):6.1%}")


if __name__ == "__main__":
    main()

