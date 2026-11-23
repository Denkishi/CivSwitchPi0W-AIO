# Build CivSwitch.exe -- a single self-contained Windows executable.
#
# From the repo root:
#     ./pc/build_exe.ps1
#
# Needs Python 3.11+ with pip. The script installs PyInstaller if missing and
# writes dist/CivSwitch.exe. No Python is needed on the machine that runs the
# resulting .exe.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

Write-Host "Installing/One-file build dependencies..."
python -m pip install --upgrade --quiet "pyinstaller>=6.11" pyserial pillow

Write-Host "Building CivSwitch.exe..."
python -m PyInstaller pc/civswitch.spec --noconfirm --distpath dist --workpath build/pyi

$exe = Join-Path $repo "dist/CivSwitch.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "Done: $exe ($mb MB)" -ForegroundColor Green
} else {
    throw "Build finished but $exe is missing."
}
