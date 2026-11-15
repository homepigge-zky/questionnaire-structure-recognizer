$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LogDirectory = Join-Path $ProjectRoot 'questionnaire_structure_recognizer\data\logs'
$StdoutLog = Join-Path $LogDirectory 'server.out.log'
$StderrLog = Join-Path $LogDirectory 'server.err.log'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment was not found: $Python"
}

$Existing = Get-NetTCPConnection -LocalPort 8200 -State Listen -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Output "Questionnaire service is already running: http://127.0.0.1:8200/ (PID $($Existing[0].OwningProcess))"
    exit 0
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$env:QSR_AUTH_MODE = 'local'
$env:QSR_DATA_DIR = 'questionnaire_structure_recognizer/data'
$env:QSR_MODEL_ENABLED = 'true'
$env:QSR_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:QSR_OLLAMA_MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
$env:QSR_MODEL_TIMEOUT = '90'
$env:QSR_BATCH_MAX_CHARS = '7000'
$env:QSR_MODEL_CONTEXT = '4096'
if (-not $env:QSR_OCR_ENABLED) { $env:QSR_OCR_ENABLED = 'false' }
$BundledLibreOffice = Join-Path $ProjectRoot '.tools\libreoffice\program\soffice.exe'
if (-not $env:LIBREOFFICE_PATH -and (Test-Path -LiteralPath $BundledLibreOffice)) {
    $env:LIBREOFFICE_PATH = $BundledLibreOffice
}

$Process = Start-Process -FilePath $Python `
    -ArgumentList @('-m','uvicorn','questionnaire_structure_recognizer.api.app:app_from_env','--factory','--host','127.0.0.1','--port','8200') `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog

Start-Sleep -Seconds 2
if ($Process.HasExited) {
    throw "Questionnaire service failed to start. Check $StderrLog"
}
Write-Output "Questionnaire service started: http://127.0.0.1:8200/ (PID $($Process.Id))"
