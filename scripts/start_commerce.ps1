param([switch]$StartWorker, [string]$Tenant = 'default', [switch]$SkipServices)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $SkipServices) { & (Join-Path $PSScriptRoot 'start_services.ps1') -ProjectRoot $project }
$python = (Get-Command python).Source
$node = (Get-Command node).Source
$env:PYTHONPATH = Join-Path $project 'src'
$logs = Join-Path $project 'data'
$processFile = Join-Path $logs 'commerce-processes.json'
$processes = @{}
if (Test-Path $processFile) {
    $saved = Get-Content $processFile -Raw | ConvertFrom-Json
    foreach ($property in $saved.PSObject.Properties) { $processes[$property.Name] = $property.Value }
}
if (-not (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue)) {
    $processes.api = (Start-Process $python -ArgumentList @('-m','uvicorn','ci_agent.api:app','--host','127.0.0.1','--port','8787') -WorkingDirectory $project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'api.stdout.log') -RedirectStandardError (Join-Path $logs 'api.stderr.log')).Id
}
if (-not (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue)) {
    $next = Join-Path $project 'frontend/node_modules/next/dist/bin/next'
    if (-not (Test-Path $next)) { throw 'Install frontend dependencies with pnpm install first.' }
    $processes.frontend = (Start-Process $node -ArgumentList @($next,'dev','--hostname','127.0.0.1','--port','3000') -WorkingDirectory (Join-Path $project 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'frontend.stdout.log') -RedirectStandardError (Join-Path $logs 'frontend.stderr.log')).Id
}
if ($StartWorker) {
    $existingWorker = $null
    if ($processes.worker) { $existingWorker = Get-CimInstance Win32_Process -Filter "ProcessId = $($processes.worker)" -ErrorAction SilentlyContinue }
    if ($existingWorker -and $existingWorker.CommandLine.Contains('monitor_worker.py')) {
        Write-Output 'The previously started worker is still running; its tenant selection is unchanged.'
    } else {
        $processes.worker = (Start-Process $python -ArgumentList @((Join-Path $project 'scripts/monitor_worker.py'),'--tenant',$Tenant) -WorkingDirectory $project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'worker.stdout.log') -RedirectStandardError (Join-Path $logs 'worker.stderr.log')).Id
    }
}
$processes | ConvertTo-Json | Set-Content $processFile -Encoding utf8
Write-Output 'Console: http://127.0.0.1:3000/commerce ; API: http://127.0.0.1:8787/docs'
if (-not $StartWorker) { Write-Output 'Worker is not started. Add -StartWorker -Tenant <tenant> to execute scheduled jobs.' }
