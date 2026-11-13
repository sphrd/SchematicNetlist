"""Build SchematicNetlist.exe: one self-contained file with Python, all libraries and the OCR
engine (RapidOCR, its PP-OCRv6 models and the ONNX runtime), so it runs on any 64-bit Windows
10/11 PC.

    .venv\\Scripts\\python.exe src\\build_exe.py

Needs the dev environment (`uv pip install -e ".[image,gui,dev]"`).
"""
import shutil
from pathlib import Path

import PyInstaller.__main__

import make_assets

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
NAME = "SchematicNetlist"
ASSETS = make_assets.ASSETS
# unused parts of big libraries: every byte is unpacked again at each start of a one-file exe
DROP_BINARIES = ("opencv_videoio_ffmpeg",)
EXCLUDE_MODULES = ["PIL._avif"]

# Splash: drawn by the bootloader BEFORE the one-file archive is unpacked (the ~9 s of "nothing"),
# its text line shows the unpack progress, gui.main() closes it once the window is mapped.
SPEC = """
from PyInstaller.utils.hooks import collect_all
datas, binaries, hidden = [], [], []
for pkg in ("tkinterdnd2", "rapidocr", "onnxruntime"):  # rapidocr: config.yaml + the .onnx models
    d, b, h = collect_all(pkg)
    datas, binaries, hidden = datas + d, binaries + b, hidden + h
a = Analysis([{script!r}], datas=datas + [({assets!r}, "schematic_netlist/assets")],
             binaries=binaries, hiddenimports=hidden, excludes={excludes!r})
a.binaries = [b for b in a.binaries if not any(d in b[0] for d in {drop!r})]
pyz = PYZ(a.pure)
splash = Splash({splash!r}, binaries=a.binaries, datas=a.datas, text_pos={text_pos!r}, text_size=10,
                text_color="#aab4c0", text_default="Starting...", minify_script=False, always_on_top=False)
exe = EXE(pyz, a.scripts, splash, splash.binaries, a.binaries, a.datas, [], name={name!r}, console=False,
          upx=False, icon={icon!r})
"""


def main() -> None:
    make_assets.main()
    BUILD.mkdir(exist_ok=True)
    spec = BUILD / f"{NAME}.spec"
    spec.write_text(SPEC.format(
        script=str(ROOT / "src" / "gui_app.py"), excludes=EXCLUDE_MODULES,
        drop=DROP_BINARIES, name=NAME, assets=str(ASSETS), splash=str(ASSETS / "splash.png"),
        text_pos=make_assets.SPLASH_TEXT_POS, icon=str(ASSETS / "icon.ico"),
    ))
    from schematic_netlist import appshell

    appshell.patch_splash_template()  # live loading log + divider sweep (App Engineer 2.4)
    PyInstaller.__main__.run([str(spec), "--noconfirm", "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD)])
    shutil.move(ROOT / "dist" / f"{NAME}.exe", ROOT / f"{NAME}.exe")
    shutil.rmtree(ROOT / "dist")
    shutil.rmtree(BUILD)
    print(f"-> {ROOT / f'{NAME}.exe'}")


if __name__ == "__main__":
    main()
