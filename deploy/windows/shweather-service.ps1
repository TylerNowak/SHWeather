<#
.SYNOPSIS
  Manage the SHWeatherService background service on Windows.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File "C:\Program Files\SHWeatherService\deploy\windows\shweather-service.ps1" status
  ... restart     (after editing C:\ProgramData\SHWeatherService\config.yaml)
  ... logs -Tail 100
#>
[CmdletBinding()]
param(
    [ValidateSet('status', 'start', 'stop', 'restart', 'logs')][string]$Action = 'status',
    [int]$Tail = 40,
    [string]$DataRoot = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not $DataRoot) { $DataRoot = $script:DefaultDataRoot }
$ConfigPath = Join-Path $DataRoot 'config.yaml'
$LogDir = Join-Path $DataRoot 'logs'
$Port = Get-ShwConfigPort $ConfigPath
$InstallDir = Get-ShwTaskInstallDir ((Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path)

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated (Administrator) PowerShell.'
    }
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host 'Not installed (no scheduled task). Run deploy\windows\install.ps1.'
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $script:TaskName
    if ([string]$task.State -eq 'Running') {
        Write-Host ("Task:       running since {0}" -f $info.LastRunTime)
    } else {
        Write-Host ("Task:       {0} (last run {1}: {2})" -f $task.State, $info.LastRunTime, (Format-ShwTaskResult $info.LastTaskResult))
    }
    $procs = @(Get-ShwProcesses)
    Write-Host ("Processes:  {0}" -f $procs.Count)
    if (Test-ShwHttp $Port) {
        Write-Host "Web:        answering on port $Port (on this PC: http://localhost:$Port)" -ForegroundColor Green
    } else {
        Write-Host "Web:        not answering on port $Port" -ForegroundColor Yellow
        if (Show-ShwDiagnosis -Port $Port -Address (Get-ShwConfigHost $ConfigPath) -LogDir $LogDir) {
            Write-Host '    Stop that program, or move SHWeather to another port by running install.ps1 again with'
            Write-Host '    -Port (for example -Port 8090): it updates config.yaml and the firewall rule.'
        }
    }
    Write-Host 'Phones:'
    $null = Show-ShwNetworkCheck $Port $InstallDir
    Write-Host "Config:     $ConfigPath"
    Write-Host "Logs:       $LogDir"
}

switch ($Action) {
    'status' { Show-Status }
    'start' {
        Assert-Admin
        Start-ScheduledTask -TaskName $script:TaskName
        Start-Sleep -Seconds 5
        Show-Status
    }
    'stop' {
        Assert-Admin
        Stop-Shw
        Write-Host 'Stopped.'
    }
    'restart' {
        Assert-Admin
        Stop-Shw
        Start-Sleep -Seconds 1
        Start-ScheduledTask -TaskName $script:TaskName
        Start-Sleep -Seconds 5
        Show-Status
    }
    'logs' {
        foreach ($name in 'shweather.log', 'stderr.log', 'supervisor.log') {
            $f = Join-Path $LogDir $name
            if (Test-Path -LiteralPath $f) {
                Write-Host "---- $f" -ForegroundColor Cyan
                Get-Content -LiteralPath $f -Tail $Tail
            }
        }
    }
}
