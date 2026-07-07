Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

pip install -e ".[build]"
python -m PyInstaller launchbox_utils.spec --noconfirm
Copy-Item dist/LaunchBoxUtils/_internal/launchbox_utils.example.ini dist/LaunchBoxUtils/launchbox_utils.example.ini

Write-Host "Built executables:"
Write-Host "  dist/LaunchBoxUtils/LaunchBoxUtils.exe (GUI, no console)"
Write-Host "  dist/LaunchBoxUtils/LaunchBoxUtils-cli.exe (CLI)"
