"""Build SchematicNetlist.exe: one self-contained file with Python, all libraries and the
Tesseract OCR engine + English model, so it runs on any 64-bit Windows 10/11 PC.

    .venv\\Scripts\\python.exe src\\build_exe.py

Needs the dev environment (`uv pip install -e ".[image,gui,dev]"`) and Tesseract installed on
the build machine (`winget install UB-Mannheim.TesseractOCR`).
"""
import shutil
import sys
import zipfile
from pathlib import Path

import PyInstaller.__main__
from PyInstaller.depend import bindepend

from schematic_netlist.image_extract import TESSERACT_ZIP, find_tesseract

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
NAME = "SchematicNetlist"
# unused parts of big libraries: every byte is unpacked again at each start of a one-file exe
DROP_BINARIES = ("opencv_videoio_ffmpeg",)
EXCLUDE_MODULES = ["PIL._avif"]

SPEC = """
from PyInstaller.utils.hooks import collect_all
datas, binaries, hidden = collect_all("tkinterdnd2")
a = Analysis([{script!r}], datas=datas + [({zip!r}, ".")], binaries=binaries,
             hiddenimports=hidden, excludes={excludes!r})
a.binaries = [b for b in a.binaries if not any(d in b[0] for d in {drop!r})]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name={name!r}, console=False, upx=False)
"""


def tesseract_files(exe: Path) -> list[Path]:
    """tesseract.exe plus every DLL of its own folder it loads (not the training tools)."""
    local = {p.name.lower(): p for p in exe.parent.iterdir() if p.is_file()}
    need: dict[str, Path] = {}
    todo = [exe.name.lower()]
    while todo:
        name = todo.pop()
        if name in need:
            continue
        need[name] = local[name]
        todo += [dep.lower() for dep, _ in bindepend.get_imports(str(local[name])) if dep.lower() in local]
    return list(need.values())


def main() -> None:
    tess = find_tesseract()
    if not tess:
        sys.exit("Tesseract not found -- install it first: winget install UB-Mannheim.TesseractOCR")
    tess = Path(tess)
    BUILD.mkdir(exist_ok=True)
    zip_path = BUILD / TESSERACT_ZIP
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in tesseract_files(tess):
            z.write(f, f.name)
        z.write(tess.parent / "tessdata" / "eng.traineddata", "tessdata/eng.traineddata")
    spec = BUILD / f"{NAME}.spec"
    spec.write_text(SPEC.format(
        script=str(ROOT / "src" / "gui_app.py"), zip=str(zip_path), excludes=EXCLUDE_MODULES,
        drop=DROP_BINARIES, name=NAME,
    ))
    PyInstaller.__main__.run([str(spec), "--noconfirm", "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD)])
    shutil.move(ROOT / "dist" / f"{NAME}.exe", ROOT / f"{NAME}.exe")
    shutil.rmtree(ROOT / "dist")
    shutil.rmtree(BUILD)
    print(f"-> {ROOT / f'{NAME}.exe'}")


if __name__ == "__main__":
    main()
