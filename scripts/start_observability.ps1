param([int]$MetricsPort = 9108)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONPATH = Join-Path $root 'src'
if (-not (Test-NetConnection 127.0.0.1 -Port $MetricsPort -InformationLevel Quiet)) {
  Start-Process python -ArgumentList '-m','ci_agent.metrics_server','--port',$MetricsPort -WorkingDirectory $root -WindowStyle Hidden | Out-Null
}
Write-Output "metrics=http://127.0.0.1:$MetricsPort/metrics"
Write-Output "grafana=docker compose --profile observability up -d prometheus grafana"
