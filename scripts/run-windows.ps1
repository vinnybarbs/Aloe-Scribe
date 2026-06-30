# run-windows.ps1 - Launch Aloe Scribe from source on Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1
#
# Use this after scripts\install-windows.ps1. It runs the app from the source
# tree (no .exe needed), which is the quickest way to test on the machine.
# Logs go to %TEMP%\aloe-scribe.log.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found. Run scripts\install-windows.ps1 first."
    exit 1
}

Write-Host "Starting Aloe Scribe..." -ForegroundColor Green
& $Python "src\main.py"
