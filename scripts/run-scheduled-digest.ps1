# Runs the publication job from Windows Task Scheduler on the same persistent
# machine that serves the web app and stores subscribers.
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonPath = Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python non trovato in $pythonPath"
}

& $pythonPath -m src.scheduled_run
exit $LASTEXITCODE
