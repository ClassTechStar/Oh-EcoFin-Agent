# 本地启动脚本（PowerShell）
# 用法：.\scripts\run_server.ps1 [-Lan] [-Demo]
param(
    [switch]$Lan,
    [switch]$Demo
)
Set-Location (Join-Path $PSScriptRoot "..")

$env:FINAGENT_LAN_MODE = "true"
if ($Lan) { $env:FINAGENT_APP__LAN_MODE = "true" } else { $env:FINAGENT_APP__LAN_MODE = "false" }
if ($Demo) { $env:DEMO_MODE = "1" }

Write-Host "启动 Oh EcoFin Agent（127.0.0.1:8765）..."
& ".\.venv\Scripts\python.exe" -m backend.app
