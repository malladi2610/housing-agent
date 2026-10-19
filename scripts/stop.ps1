$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pidPath = Join-Path $projectRoot 'data\windows-processes.json'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host 'No launcher process record found.'; exit 0 }
$entries = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
foreach ($entry in $entries) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.id)" -ErrorAction SilentlyContinue
    if ($process -and $process.ExecutablePath -eq $pythonPath -and $process.CommandLine -match 'flat_agent.cli') {
        Stop-Process -Id $entry.id
        Write-Host "Stopped Homeward $($entry.role). Queued work remains in the database."
    }
}
Remove-Item -LiteralPath $pidPath
