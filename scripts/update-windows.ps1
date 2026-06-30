# update-windows.ps1 - Update Aloe Scribe to the latest version on Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\update-windows.ps1
#
# Pulls the latest code, refreshes dependencies, and makes sure the local model
# is present. Preserves config\config.toml (your settings).

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "[1/3] Pulling the latest version from GitHub..." -ForegroundColor Green
git pull --ff-only origin main

$Python = ".venv\Scripts\python.exe"
$Pip = ".venv\Scripts\pip.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found. Run scripts\install-windows.ps1 first."
    exit 1
}

Write-Host "[2/3] Refreshing dependencies..." -ForegroundColor Green
& $Pip install -r requirements-windows.txt --quiet

Write-Host "[3/3] Ensuring the local model is present..." -ForegroundColor Green
& powershell -ExecutionPolicy Bypass -File "scripts\fetch-model-windows.ps1"

Write-Host ""
Write-Host "Update complete." -ForegroundColor Green
Write-Host "  Launch with:  scripts\run-windows.ps1"
