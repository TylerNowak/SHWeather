<#
.SYNOPSIS
  Supervisor started at boot by the SHWeatherService scheduled task. Not meant to be run by hand.
.DESCRIPTION
  Runs "shweather serve" and restarts it if it ever exits, backing off up to 5 minutes when
  it keeps failing. (Task Scheduler's own "restart on failure" only covers a task that
  fails to start, not a program that exits later.) The server writes its own rotating log;
  this script keeps the last run's stderr (crash tracebacks) and a short restart log.
#>
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$Config
)
$ErrorActionPreference = 'Continue'

$exe = Join-Path $InstallDir '.venv\Scripts\shweather.exe'
$logDir = Join-Path (Split-Path -Parent $Config) 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stderr = Join-Path $logDir 'stderr.log'
$stdout = Join-Path $logDir 'stdout.log'
$restarts = Join-Path $logDir 'supervisor.log'

function Write-Supervisor([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $restarts -Value $line -Encoding ASCII
    # keep the restart log small
    $lines = @(Get-Content -LiteralPath $restarts -ErrorAction SilentlyContinue)
    if ($lines.Count -gt 500) { $lines | Select-Object -Last 250 | Set-Content -LiteralPath $restarts -Encoding ASCII }
}

$delay = 5
while ($true) {
    if (-not (Test-Path -LiteralPath $exe)) {
        Write-Supervisor "missing $exe - reinstall with deploy\windows\install.ps1"
        Start-Sleep -Seconds 300
        continue
    }
    if (Test-Path -LiteralPath $stderr) {
        Move-Item -LiteralPath $stderr -Destination (Join-Path $logDir 'stderr.previous.log') -Force
    }
    $started = Get-Date
    Write-Supervisor 'starting shweather'
    $proc = Start-Process -FilePath $exe -ArgumentList @('--config', ('"{0}"' -f $Config), 'serve') `
        -WorkingDirectory (Split-Path -Parent $Config) -NoNewWindow -PassThru `
        -RedirectStandardError $stderr -RedirectStandardOutput $stdout
    $null = $proc.Handle   # cache the handle so ExitCode is available after the process exits
    $proc.WaitForExit()
    $ran = ((Get-Date) - $started).TotalSeconds
    if ($ran -gt 300) { $delay = 5 } else { $delay = [Math]::Min($delay * 2, 300) }
    Write-Supervisor ("shweather exited with code {0} after {1:N0} s; restarting in {2} s" -f $proc.ExitCode, $ran, $delay)
    Start-Sleep -Seconds $delay
}
