# fetch-model-windows.ps1 - Ensure the faster-whisper model is present locally
# (from GitHub, NOT Hugging Face) and point config\config.toml at it.
#
# Idempotent: if the model is already in models\<name> it just makes sure the
# config path is set and exits. Called by install-windows.ps1 and update-windows.ps1.
#
#   powershell -ExecutionPolicy Bypass -File scripts\fetch-model-windows.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Repo = "vinnybarbs/Aloe-Scribe"
$ModelName = "faster-distil-whisper-large-v3"
$ModelDir = Join-Path $RepoRoot "models\$ModelName"
$Tag = "model-$ModelName"
$Base = "https://github.com/$Repo/releases/download/$Tag"
$Config = "config\config.toml"

New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null

$haveModel = (Test-Path (Join-Path $ModelDir "model.bin")) -and `
             (Test-Path (Join-Path $ModelDir "config.json"))

if ($haveModel) {
    Write-Host "  faster-whisper model already present: $ModelDir"
} else {
    Write-Host "  Downloading faster-whisper model from GitHub (no Hugging Face)..."
    # Manifest first, so we know exactly which files to fetch and the expected hash.
    Invoke-WebRequest -Uri "$Base/SHA256SUMS" -OutFile (Join-Path $ModelDir "SHA256SUMS")

    $sums = @{}
    Get-Content (Join-Path $ModelDir "SHA256SUMS") | ForEach-Object {
        $line = $_.Trim()
        if ($line) {
            $parts = $line -split '\s+', 2
            $hash = $parts[0]
            $file = $parts[1].Trim()
            if ($file -ne "SHA256SUMS") { $sums[$file] = $hash }
        }
    }

    foreach ($file in $sums.Keys) {
        Write-Host "    $file ..."
        Invoke-WebRequest -Uri "$Base/$file" -OutFile (Join-Path $ModelDir $file)
    }

    # Verify the big weight file end to end.
    $expected = $sums["model.bin"]
    $actual = (Get-FileHash (Join-Path $ModelDir "model.bin") -Algorithm SHA256).Hash.ToLower()
    if ($expected -and ($expected.ToLower() -ne $actual)) {
        Remove-Item (Join-Path $ModelDir "model.bin") -Force
        Write-Error "Model checksum mismatch - download corrupt. Re-run to retry."
        exit 1
    }
    Write-Host "  Model verified: $ModelDir"
}

# Point the app at the local model path (replaces any Hugging Face id) and keep
# it fully offline.
if (Test-Path $Config) {
    $cfg = Get-Content $Config -Raw
    $modelEsc = $ModelDir -replace '\\', '\\'
    if ($cfg -match '(?m)^\s*faster_whisper_model\s*=') {
        $cfg = $cfg -replace '(?m)^\s*faster_whisper_model\s*=\s*".*?"', "faster_whisper_model = `"$modelEsc`""
    } else {
        $cfg = $cfg -replace '(?m)^(backend\s*=\s*"faster_whisper")', "`$1`nfaster_whisper_model = `"$modelEsc`""
    }
    Set-Content -Path $Config -Value $cfg -NoNewline
    Write-Host "  config.toml points at the local model."
}
