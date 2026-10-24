# VideoCut startup script
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# 优先使用本工程 .venv，否则用 PATH 里的 python
$Python = "python"
if (Test-Path ".venv\Scripts\python.exe") {
    $Python = ".venv\Scripts\python.exe"
}

Write-Host "Starting VideoCut..." -ForegroundColor Cyan
Write-Host "Using interpreter: $Python"
& $Python "funclip/launch.py" --home slirn @args
