# build-installer-windows.ps1 - Build the Aloe Scribe Windows installer.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-installer-windows.ps1
#
# Produces installer\Output\AloeScribeSetup.exe: a single double-click installer
# that bundles the app and the transcription model, with shortcuts and an
# uninstaller. Needs Inno Setup 6 installed (https://jrsoftware.org/isdl.php) or
# available via "choco install innosetup".

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = ".venv\Scripts\python.exe"
$Pip = ".venv\Scripts\pip.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found. Run scripts\install-windows.ps1 first."
    exit 1
}

Write-Host "[1/6] Ensuring build dependencies..." -ForegroundColor Green
& $Pip install -r requirements-windows.txt --quiet
& $Pip install pyinstaller==6.11.1 --quiet

Write-Host "[2/6] Fetching the model to bundle..." -ForegroundColor Green
& powershell -ExecutionPolicy Bypass -File "scripts\fetch-model-windows.ps1"

Write-Host "[3/6] Setting the bundled config..." -ForegroundColor Green
$Config = "config\config.toml"
if (-not (Test-Path $Config)) { Copy-Item "config\config.toml.example" $Config }
$cfg = Get-Content $Config -Raw
$cfg = $cfg -replace '(?m)^\s*backend\s*=\s*".*?"', 'backend = "faster_whisper"'
# Bare model name on purpose: the app resolves it to the model folder next to the
# installed .exe (the absolute build-machine path would not exist on the user's PC).
if ($cfg -match '(?m)^\s*faster_whisper_model\s*=') {
    $cfg = $cfg -replace '(?m)^\s*faster_whisper_model\s*=\s*".*?"', 'faster_whisper_model = "faster-distil-whisper-large-v3"'
} else {
    $cfg = $cfg -replace '(?m)^(backend\s*=\s*"faster_whisper")', "`$1`nfaster_whisper_model = `"faster-distil-whisper-large-v3`""
}
if ($cfg -notmatch '(?m)^\s*faster_whisper_device\s*=') {
    $cfg = $cfg -replace '(?m)^(faster_whisper_model\s*=.*)', "`$1`nfaster_whisper_device = `"auto`""
}
$cfg = $cfg -replace '(?m)^\s*local_dir\s*=\s*".*?"', 'local_dir = "~/meetings"'
Set-Content -Path $Config -Value $cfg -NoNewline

Write-Host "[4/6] Generating icon.ico..." -ForegroundColor Green
if (-not (Test-Path "assets\icon.ico")) {
    & $Python -c "from PIL import Image; Image.open('assets/icon.png').save('assets/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
}

Write-Host "[5/6] Building the app with PyInstaller..." -ForegroundColor Green
& $Python -m PyInstaller --noconfirm --clean aloe-scribe-windows.spec

Write-Host "[6/6] Compiling the installer with Inno Setup..." -ForegroundColor Green
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    $iscc = "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $iscc)) {
    Write-Error "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php or 'choco install innosetup', then re-run."
    exit 1
}
& $iscc "installer\aloe-scribe.iss"

Write-Host ""
Write-Host "Installer built." -ForegroundColor Green
Write-Host "  installer\Output\AloeScribeSetup.exe"
Write-Host "  Double-click it to install. It is unsigned, so Windows SmartScreen"
Write-Host "  will show 'More info' then 'Run anyway' the first time."
