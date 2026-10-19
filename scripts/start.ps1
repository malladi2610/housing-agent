$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run .\scripts\setup.ps1 first.' }
Set-Location -LiteralPath $projectRoot
$stateDir = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$pidPath = Join-Path $stateDir 'windows-processes.json'
if (Test-Path -LiteralPath $pidPath) {
    $priorProcesses = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    foreach ($entry in $priorProcesses) {
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.id)" -ErrorAction SilentlyContinue
        if ($existing -and $existing.ExecutablePath -eq $pythonPath -and $existing.CommandLine -match 'flat_agent.cli') {
            throw 'A previous Homeward process is still running. Use .\scripts\stop.ps1 before restarting.'
        }
    }
}
$started = @()
try {
    foreach ($role in @('web','worker')) {
        $process = Start-Process -FilePath $pythonPath -ArgumentList @('-m','flat_agent.cli',$role) -WorkingDirectory $projectRoot -RedirectStandardOutput (Join-Path $stateDir "$role.stdout.log") -RedirectStandardError (Join-Path $stateDir "$role.stderr.log") -PassThru -WindowStyle Hidden
        $started += [pscustomobject]@{ id=$process.Id; role=$role }
    }
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
    Write-Host 'Web and worker started. Open http://127.0.0.1:8000. Logs are in ignored data/.'
    Write-Host 'If FLAT_UI_PASSWORD is set, sign in with username owner.'
    Start-Process 'http://127.0.0.1:8000'
} catch {
    foreach ($entry in $started) { Stop-Process -Id $entry.id -ErrorAction SilentlyContinue }
    throw
}
