# Build SchematicNetlist.exe into the project root (one portable file: Python, libraries, Tesseract OCR).
#
#   .\build.ps1            # build
#   .\build.ps1 -Setup     # first time: create .venv and install the build dependencies, then build
#
# Needs Tesseract on the build machine (winget install UB-Mannheim.TesseractOCR); it is packed into the exe.
param([switch]$Setup)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($Setup -or -not (Test-Path $venvPy)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv .venv
        uv pip install --python $venvPy -e ".[gui,image]" pyinstaller
    } else {
        py -m venv .venv
        & $venvPy -m pip install -e ".[gui,image]" pyinstaller
    }
}

# Some company policies block .venv\Scripts\python.exe: fall back to the py launcher with the venv's packages
$py = $venvPy
& $py -c "import sys" 2>$null
if ($LASTEXITCODE -ne 0) {
    $py = "py"
    $env:PYTHONPATH = (Join-Path $PSScriptRoot ".venv\Lib\site-packages") + ";" + (Join-Path $PSScriptRoot "src")
}

$exe = Join-Path $PSScriptRoot "SchematicNetlist.exe"
$started = Get-Date
& $py (Join-Path $PSScriptRoot "src\build_exe.py")
if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }
if (-not (Test-Path $exe) -or (Get-Item $exe).LastWriteTime -lt $started) { throw "build ran but $exe was not written" }
"{0}  {1:N0} MB" -f $exe, ((Get-Item $exe).Length / 1MB)
