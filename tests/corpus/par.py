"""Parallel scorer: dev / hold-out means. Usage: par.py [base|var|all] [-v] [--big]"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
ENG = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--engine=")), str(Path(__file__).resolve().parents[2] / "src"))
DEV = ["rectifier", "amplifier-ac", "buck_conv", "Class-D", "ecc83-pp", "generic_opamp_bip", "hv_converter", "laser_driver",
       "mainsheet", "multichannel_mixer", "pic_programmer", "power_supply", "Q17ng", "royer1", "sallen_key", "smps-com",
       "StickHub", "tinytapeout-demo", "complex_hierarchy", "CM5_MINIMA_3", "video"]
HOLD = ["a-multi", "ecc83-pp_v2", "ibis", "interf_u", "mult_vca810", "One-Air-Max", "RoyalBlue54L-Feather",
        "RoyalBlue54L-NFC-Antenna", "sonde_xilinx", "up-down-c", "v_i_sources"]
BIG = ["jetson-agx-thor-baseboard", "kit-dev-coldfire-xilinx_5213", "vme-wren"]


def run(name):
    import score
    try:
        return score.score_case(HERE / "cases" / name, ENG)
    except Exception as e:  # noqa: BLE001
        return {"case": name, "error": repr(e)}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "base"
    verbose = "-v" in sys.argv
    projs = DEV + HOLD + (BIG if "--big" in sys.argv else [])
    sfx = {"base": [""], "var": ["__bw", "__x0.4", "__x3"], "all": ["", "__bw", "__x0.4", "__x3"]}[mode]
    names = [p + s for p in projs for s in sfx]
    with ProcessPoolExecutor(8) as ex:
        res = dict(zip(names, ex.map(run, names)))
    groups = {"DEV": [], "HOLD": [], "BIG": []}
    for nm, r in res.items():
        if "error" in r:
            print(f"{nm:34s} ERROR {r['error']}")
            continue
        base = nm.split("__")[0]
        groups["DEV" if base in DEV else "HOLD" if base in HOLD else "BIG"].append(r)
        print(f"{nm:34s} parts {r['parts']:6.1%} ({r['gt_parts']:3d}, +{r['extra_parts']:2d})  pins {r['pins']:6.1%}  "
              f"connP {r['connP']:6.1%}  connR {r['connR']:6.1%}  {r['secs']:5.1f}s")
        if verbose:
            print(f"    missing {r['missing_refs']}\n    extra   {r['extra_refs']}")
    for g, rows in groups.items():
        if rows:
            avg = lambda k: sum(r[k] for r in rows) / len(rows)  # noqa: E731
            print(f"{g:5s} n={len(rows):3d} parts {avg('parts'):6.1%}  pins {avg('pins'):6.1%}  connP {avg('connP'):6.1%}  "
                  f"connR {avg('connR'):6.1%}  extra {sum(r['extra_parts'] for r in rows)}")


if __name__ == "__main__":
    main()
