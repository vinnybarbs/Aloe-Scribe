# build-windows.ps1 - Build a standalone Aloe Scribe.exe with PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
#
# Run scripts\install-windows.ps1 first. Output lands in dist\Aloe Scribe\.
#
# Packaging faster-whisper and PyAV is fiddly; if the built .exe fails on
# launch with a missing module or data file, add it to hiddenimports / datas in
# aloe-scribe-windows.spec and re-run this. Iterate on the Windows machine.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found. Run scripts\install-windows.ps1 first."
    exit 1
}

Write-Host "[1/3] Ensuring PyInstaller is installed..." -ForegroundColor Green
& $Python -m pip install --quiet pyinstaller==6.11.1

Write-Host "[2/3] Generating icon.ico from icon.png..." -ForegroundColor Green
$ico = "assets\icon.ico"
if (-not (Test-Path $ico)) {
    & $Python -c "from PIL import Image; Image.open('assets/icon.png').save('assets/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
    Write-Host "  Wrote $ico"
} else {
    Write-Host "  $ico already present."
}

# The bundle ships only config.toml.example. A personal config\config.toml on
# the build machine must never reach the app — the runtime seeds each user's
# config in %APPDATA% from the template on first launch.

Write-Host "[3/3] Building with PyInstaller (a few minutes)..." -ForegroundColor Green
& $Python -m PyInstaller --noconfirm --clean aloe-scribe-windows.spec

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "  App: dist\Aloe Scribe\Aloe Scribe.exe"
Write-Host "  Double-click it, or pin it to the Start menu."
