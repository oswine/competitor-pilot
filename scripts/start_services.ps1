param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = 'Stop'
$dataRoot = Join-Path $ProjectRoot 'data'
$pgRoot = Join-Path $ProjectRoot 'services\postgresql\pgsql'
$pgBin = Join-Path $pgRoot 'bin'
$pgData = Join-Path $dataRoot 'postgres'
$pgLog = Join-Path $dataRoot 'postgres.log'
$minioData = Join-Path $dataRoot 'minio'
$pidFile = Join-Path $dataRoot 'service-pids.json'

New-Item -ItemType Directory -Force $dataRoot, $minioData | Out-Null

if (-not (Test-Path (Join-Path $pgBin 'initdb.exe'))) {
    throw "Portable PostgreSQL not found at $pgRoot. Extract postgresql-*-binaries.zip there first."
}

if (-not (Test-Path (Join-Path $pgData 'PG_VERSION'))) {
    & (Join-Path $pgBin 'initdb.exe') -D $pgData -U ci -A trust --no-locale
}

if (-not (Test-NetConnection 127.0.0.1 -Port 5432 -InformationLevel Quiet)) {
    & (Join-Path $pgBin 'pg_ctl.exe') -D $pgData -l $pgLog -o '-p 5432' start | Out-Host
}

$minio = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter minio.exe -ErrorAction SilentlyContinue | Where-Object Length -gt 1000000 | Select-Object -First 1
if ($null -eq $minio) { throw 'MinIO was not found through WinGet.' }
if (-not (Test-NetConnection 127.0.0.1 -Port 9000 -InformationLevel Quiet)) {
    $env:MINIO_ROOT_USER = 'ci-minio'
    $env:MINIO_ROOT_PASSWORD = 'ci-minio-secret'
    $p = Start-Process $minio.FullName -ArgumentList @('server', $minioData, '--address', '127.0.0.1:9000', '--console-address', '127.0.0.1:9001') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
    $minioPid = $p.Id
}

$jaeger = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter jaeger.exe -ErrorAction SilentlyContinue | Where-Object Length -gt 1000000 | Select-Object -First 1
if ($null -eq $jaeger) { throw 'Jaeger was not found through WinGet.' }
if (-not (Test-NetConnection 127.0.0.1 -Port 16686 -InformationLevel Quiet)) {
    $p = Start-Process $jaeger.FullName -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
    $jaegerPid = $p.Id
}

Start-Sleep -Seconds 2
$checks = @{
    postgres = (Test-NetConnection 127.0.0.1 -Port 5432 -InformationLevel Quiet)
    minio = (Test-NetConnection 127.0.0.1 -Port 9000 -InformationLevel Quiet)
    jaeger = (Test-NetConnection 127.0.0.1 -Port 16686 -InformationLevel Quiet)
}
if ($checks.Values -contains $false) { throw "Service health check failed: $($checks | ConvertTo-Json -Compress)" }
@{ postgres_pid = $null; minio_pid = $minioPid; jaeger_pid = $jaegerPid; started_at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
$checks | ConvertTo-Json
