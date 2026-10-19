$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if ($env:OS -ne 'Windows_NT') { throw 'This setup script is for Windows.' }
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer, then retry.' }
    & py -3 -m venv .venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer, then retry.' }
    & python -m venv .venv
} else { throw 'Install Python 3.11+ from python.org, then retry.' }
if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $pythonPath -m pip install uv
if ($LASTEXITCODE -ne 0) { throw 'Could not install the dependency manager.' }
& $pythonPath -m uv sync --frozen --extra test
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check internet access and TLS configuration.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
Write-Host 'Setup complete. Run .\scripts\start.ps1, then open Models & settings to connect Ollama Cloud.'
