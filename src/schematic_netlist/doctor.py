"""Environment self-check (`schematic-netlist doctor`): required/optional dependencies, the
Tesseract OCR engine, and where the installed code actually lives, with fix instructions."""
from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 10)


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "missing"
    detail: str
    fix_hint: str = ""

    @property
    def symbol(self) -> str:
        return {"ok": "[OK]  ", "warn": "[WARN]", "missing": "[MISS]"}[self.status]


def _check_python() -> Check:
    version = sys.version_info[:3]
    if sys.version_info[:2] >= MIN_PYTHON:
        return Check("Python", "ok", f"{'.'.join(map(str, version))} (>= {'.'.join(map(str, MIN_PYTHON))} required)")
    return Check(
        "Python",
        "missing",
        f"{'.'.join(map(str, version))} is older than the required {'.'.join(map(str, MIN_PYTHON))}",
        "Install Python 3.10+ and recreate the virtual environment.",
    )


def _check_module(module: str, package_hint: str, required: bool) -> Check:
    try:
        mod = importlib.import_module(module)
        version = getattr(mod, "__version__", "unknown version")
        return Check(module, "ok", version)
    except ImportError:
        status = "missing" if required else "warn"
        extra = "" if required else " (only needed for scanned/image-only PDF pages)"
        return Check(
            module,
            status,
            f"not importable{extra}",
            'Install with:  uv pip install -e ".[image]"' if not required else f"Install with:  uv pip install -e . ({package_hint})",
        )


def _check_tesseract() -> Check:
    from .image_extract import find_tesseract

    path = find_tesseract()
    if path is None:
        return Check(
            "tesseract (OCR engine)",
            "warn",
            "not found (only needed for scanned/image-only PDF pages)",
            "Install with:  winget install UB-Mannheim.TesseractOCR",
        )
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
        first_line = result.stdout.splitlines()[0] if result.stdout else path
        return Check("tesseract (OCR engine)", "ok", first_line)
    except Exception as e:  # pragma: no cover -- defensive, binary found but unrunnable
        return Check("tesseract (OCR engine)", "warn", f"found at {path} but failed to run: {e}")


def _check_source_location() -> Check:
    """A copied (not moved) project folder with a stale .venv silently runs the old copy's code:
    warn when the cwd looks like this project but the imported package lives elsewhere."""
    import schematic_netlist

    project_from_package = Path(schematic_netlist.__file__).resolve().parents[2]
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and cwd != project_from_package:
        return Check(
            "source location",
            "warn",
            f"running from {cwd} but the installed package's code actually lives at {project_from_package}",
            "This usually means the project folder was copied (not moved) after the last install. "
            "Delete .venv here and run `uv venv; uv pip install -e \".[image]\"` from this folder.",
        )
    return Check("source location", "ok", str(project_from_package))


def run_checks() -> list[Check]:
    checks = [_check_python(), _check_module("pdfplumber", "base dependency", required=True)]
    checks.append(_check_module("openpyxl", "base dependency", required=True))
    checks.append(_check_module("cv2", "image extra", required=False))
    checks.append(_check_module("pytesseract", "image extra", required=False))
    checks.append(_check_module("numpy", "image extra", required=False))
    checks.append(_check_tesseract())
    checks.append(_check_source_location())
    return checks


def format_report(checks: list[Check]) -> str:
    lines = ["schematic-netlist environment check", "=" * 36]
    for c in checks:
        lines.append(f"{c.symbol} {c.name}: {c.detail}")
        if c.status != "ok" and c.fix_hint:
            lines.append(f"       -> {c.fix_hint}")
    missing_required = [c for c in checks if c.status == "missing"]
    if missing_required:
        lines.append("")
        lines.append("Required dependencies are missing -- `analyze` will not work.")
    else:
        image_ready = all(c.status == "ok" for c in checks if c.name in {"cv2", "pytesseract"} or "tesseract" in c.name)
        lines.append("")
        if image_ready:
            lines.append("Everything is ready, including scanned/image-only PDF pages.")
        else:
            lines.append(
                "Core features are ready. Scanned/image-only PDF pages need the items marked "
                "[WARN]/[MISS] above -- `uv pip install -e \".[image]\"` and "
                "`winget install UB-Mannheim.TesseractOCR`."
            )
    return "\n".join(lines)
