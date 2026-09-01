param([string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path)
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $ProjectRoot 'data\service-pids.json'
if (Test-Path $pidFile) {
    $record = Get-Content $pidFile -Raw | ConvertFrom-Json
    foreach ($name in @('minio_pid','jaeger_pid')) {
        $id = [int]$record.$name
        if ($id -gt 0) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
}
$pg = Join-Path $ProjectRoot 'services\postgresql\pgsql\bin\pg_ctl.exe'
$data = Join-Path $ProjectRoot 'data\postgres'
if (Test-Path $pg) { & $pg -D $data stop -m fast 2>$null | Out-Host }
