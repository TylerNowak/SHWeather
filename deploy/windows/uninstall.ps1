<#
.SYNOPSIS
  Remove SHWeatherService from Windows. Keeps config, database and logs unless -RemoveData.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1
  powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -RemoveData
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [string]$InstallDir = '',
    [string]$DataRoot = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $InstallDir) { $InstallDir = $script:DefaultInstallDir }
if (-not $DataRoot) { $DataRoot = $script:DefaultDataRoot }

Write-Step 'Stopping the service'
Stop-Shw

Write-Step 'Removing the scheduled task'
if (Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false
}

Write-Step 'Removing firewall rules'
Get-NetFirewallRule -Group $script:FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule

Set-Location -LiteralPath $env:TEMP
if (Test-Path -LiteralPath (Join-Path $InstallDir '.git')) {
    # Installed with -InstallDir pointing at a git checkout: never delete the repository.
    Write-Host "Leaving $InstallDir alone (it is a git checkout); delete its .venv folder if you like."
} else {
    Write-Step "Removing $InstallDir"
    Reset-ShwAcl $InstallDir   # in case an older installer locked the permissions down
    if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
}

if ($RemoveData) {
    Write-Step "Removing $DataRoot (config, database, logs)"
    Reset-ShwAcl $DataRoot
    if (Test-Path -LiteralPath $DataRoot) { Remove-Item -LiteralPath $DataRoot -Recurse -Force }
} else {
    Write-Host "Kept config, database and logs in $DataRoot (use -RemoveData to delete them)."
}
Write-Host 'SHWeatherService removed.' -ForegroundColor Green
