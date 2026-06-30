# install-windows.ps1 - Set up Aloe Scribe to run on Windows from source.
#
# Creates a local Python venv, installs the Windows dependencies, and seeds
# config\config.toml with the faster-whisper backend. Run from the repo root in
# PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#
# After it finishes, launch with:  scripts\run-windows.ps1
#
# This does NOT build an .exe (that is build-windows.ps1). Running from source
# is the quickest way to confirm audio capture and transcription work on the
# machine before packaging.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "[1/4] Checking Python..." -ForegroundColor Green
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "Python not found. Install Python 3.10-3.12 from python.org and re-run."
    exit 1
}
$ver = (python -c "import sys; print('%d.%d' % sys.version_info[:2])")
Write-Host "  Python $ver"

Write-Host "[2/5] Creating virtual environment (.venv)..." -ForegroundColor Green
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
$Pip = ".venv\Scripts\pip.exe"
$Python = ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip --quiet

Write-Host "[3/5] Installing Windows dependencies (faster-whisper, PyQt6, WASAPI)..." -ForegroundColor Green
Write-Host "      First run downloads PyTorch-free wheels, a few minutes."
& $Pip install -r requirements-windows.txt

Write-Host "[4/5] Seeding config\config.toml..." -ForegroundColor Green
$Config = "config\config.toml"
$Template = "config\config.toml.example"
if (-not (Test-Path $Config)) {
    Copy-Item $Template $Config
    Write-Host "  Created config\config.toml from the template."
} else {
    Write-Host "  Keeping existing config\config.toml."
}

# Point the config at the Windows backend. Replace the backend line and make
# sure the faster-whisper keys exist. fetch-model (step 5) sets the model path
# to the local folder it downloads.
$cfg = Get-Content $Config -Raw
$cfg = $cfg -replace '(?m)^\s*backend\s*=\s*".*?"', 'backend = "faster_whisper"'
if ($cfg -notmatch '(?m)^\s*faster_whisper_device\s*=') {
    $cfg = $cfg -replace '(?m)^(backend\s*=\s*"faster_whisper")',
        "`$1`nfaster_whisper_device = `"auto`""
}
# Default the transcript output folder to %USERPROFILE%\meetings.
$MeetingsDir = Join-Path $env:USERPROFILE "meetings"
$MeetingsEsc = $MeetingsDir -replace '\\', '\\'
$cfg = $cfg -replace '(?m)^\s*local_dir\s*=\s*".*?"', "local_dir = `"$MeetingsEsc`""
Set-Content -Path $Config -Value $cfg -NoNewline
New-Item -ItemType Directory -Force -Path $MeetingsDir | Out-Null

Write-Host "[5/5] Fetching the model from GitHub (no Hugging Face, ~1.4 GB)..." -ForegroundColor Green
& powershell -ExecutionPolicy Bypass -File "scripts\fetch-model-windows.ps1"

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "  Transcripts will save to: $MeetingsDir"
Write-Host "  The model is local, so transcription works fully offline."
Write-Host "  Launch the app with:  scripts\run-windows.ps1"
