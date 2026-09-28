<#
.SYNOPSIS
  Install (or upgrade) SHWeatherService on Windows 10/11 as a background service that
  starts at boot, before anyone logs in.

.DESCRIPTION
  - Copies the app to "C:\Program Files\SHWeatherService" with its own Python virtualenv.
  - Keeps config, database and logs in "C:\ProgramData\SHWeatherService" (kept on upgrade).
  - Registers a boot-time scheduled task running as SYSTEM, supervised by run-service.ps1.
  - Opens the web port in Windows Defender Firewall (private/domain networks by default).

  Run from an elevated PowerShell in the repository folder:
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1

.PARAMETER Port
  Web/API port. Default 8080 for a new install; on an upgrade the port in the existing
  config.yaml is kept unless -Port is given, which changes it there (and in the firewall).
.PARAMETER NmeaUdpPort
  Also open these UDP ports, for NMEA multiplexers that broadcast (usually 10110).
.PARAMETER AllowPublicNetworks
  Also accept connections on networks Windows classifies as Public. Boat routers are often
  classified as Public; marina Wi-Fi should stay closed unless you set api_token.
.PARAMETER NoSerial
  Skip pyserial (NMEA over USB/COM ports).
.PARAMETER Python
  Path to python.exe (3.11+) to build the virtualenv from. Found automatically otherwise.
.PARAMETER InstallDir
  Where the app lives. Pass your git checkout (e.g. D:\Git\SHWeather) to run the
  service straight from it: nothing is copied, and code changes apply after a restart.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -NmeaUdpPort 10110 -AllowPublicNetworks
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Port 8090
  (another program already uses port 8080)
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8080,
    [int[]]$NmeaUdpPort = @(),
    [switch]$AllowPublicNetworks,
    [switch]$NoSerial,
    [string]$Python = '',
    [string]$InstallDir = '',
    [string]$DataRoot = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $InstallDir) { $InstallDir = $script:DefaultInstallDir }
if (-not $DataRoot) { $DataRoot = $script:DefaultDataRoot }
$Source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ConfigPath = Join-Path $DataRoot 'config.yaml'
$started = Get-Date
# An existing config.yaml decides the port, unless -Port is given (which then updates it).
$portGiven = $PSBoundParameters.ContainsKey('Port')
if (-not $portGiven) { $Port = Get-ShwConfigPort $ConfigPath $Port }
$bindHost = Get-ShwConfigHost $ConfigPath

function Find-Python([string]$Preferred) {
    $candidates = New-Object System.Collections.ArrayList
    if ($Preferred) { [void]$candidates.Add(@($Preferred)) }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    # "-3" is the newest installed 3.x (covers 3.14+); then specific versions in case the
    # newest is too old or broken.
    if ($py) { foreach ($v in '3', '3.13', '3.12', '3.11') { [void]$candidates.Add(@($py.Source, "-$v")) } }
    foreach ($name in 'python.exe', 'python3.exe') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { [void]$candidates.Add(@($cmd.Source)) }
    }
    foreach ($cand in $candidates) {
        $exe = $cand[0]
        $extra = @($cand | Select-Object -Skip 1)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'   # Windows PowerShell turns native stderr into errors
        try {
            $out = @(& $exe @extra -c 'import sys; print(sys.version_info >= (3, 11)); print(sys.executable)' 2>$null)
        } catch {
            continue
        } finally {
            $ErrorActionPreference = $prev
        }
        if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and $out[0].Trim() -eq 'True') {
            return $out[1].Trim()
        }
    }
    return $null
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$What) {
    # Judge native tools by exit code only: pip prints harmless warnings on stderr.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# ---------------------------------------------------------------- Python
Write-Step 'Looking for Python 3.11 or newer'
$pythonExe = Find-Python $Python
if (-not $pythonExe) {
    throw ("Python 3.11+ not found. Install it for all users, then re-run this script:`n" +
           "  winget install -e --id Python.Python.3.12 --scope machine`n" +
           "or download it from https://www.python.org/downloads/windows/ and tick 'Install for all users'.")
}
if ($pythonExe -like '*\WindowsApps\*') {
    throw ("The Microsoft Store build of Python cannot be used by a background service.`n" +
           "Install Python for all users instead:  winget install -e --id Python.Python.3.12 --scope machine")
}
Write-Host "    using $pythonExe"
if ($pythonExe -like "$($env:SystemDrive)\Users\*") {
    Write-Warning ("This Python is installed for one user only. The service will work, but breaks if that user " +
                   "uninstalls it. Prefer:  winget install -e --id Python.Python.3.12 --scope machine")
}

# ---------------------------------------------------------------- stop an older version
$wasInstalled = [bool](Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue)
if ($wasInstalled -or @(Get-ShwProcesses).Count -gt 0) {
    # Also covers a half-finished earlier install that left the server running without a task:
    # running files can't be replaced.
    Write-Step 'Stopping the running service for the upgrade'
    Stop-Shw
}

# Running straight from the git checkout? Then nothing is copied or cleaned up.
$inPlace = ((Resolve-Path $Source).Path.TrimEnd('\') -eq $InstallDir.TrimEnd('\'))
# Only what the server needs is installed; development folders stay in the repo.
$devOnly = @('.git', '.github', 'tests', 'node_modules')

# ---------------------------------------------------------------- the web port
# Checked before anything is copied: on a port another program holds, the service would
# keep restarting without ever answering. (An older version stays stopped: it could not
# have been answering on this port either.)
Write-Step "Checking that port $Port is free"
$portState = Wait-ShwPortFree $Port $bindHost
if ($portState -eq 'InUse' -or $portState -eq 'Reserved') {
    $free = Find-ShwFreePort $bindHost
    $again = [ordered]@{}
    foreach ($k in $PSBoundParameters.Keys) { $again[$k] = $PSBoundParameters[$k] }
    $again['Port'] = $(if ($free) { $free } else { 8090 })
    $retry = Format-ShwCommand $PSCommandPath $again
    if ($portState -eq 'InUse') {
        $owners = @(Get-ShwPortOwners $Port)
        Write-Host "    Port $Port is already in use by:" -ForegroundColor Yellow
        if ($owners.Count -gt 0) { $owners | ForEach-Object { Write-Host ('      ' + (Format-ShwPortOwner $_)) } }
        else { Write-Host '      (could not tell which program)' }
        $why = @("Port $Port is already used by another program, and two programs cannot share a port.",
                 'Either stop that program (and keep it from starting at boot) and run this installer again,',
                 'or install SHWeather on a free port')
    } else {
        $why = @("Windows does not allow programs to use port ${Port}: it is in a reserved range, often kept by",
                 'Hyper-V, WSL or Docker (list them with: netsh interface ipv4 show excludedportrange protocol=tcp).',
                 'Install SHWeather on a free port')
    }
    if ($free) { $why[-1] += " ($free is free)" }
    $why[-1] += ':'
    Write-Host ''
    $why | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host "    $retry"
    Write-Host ''
    if ($wasInstalled) {
        Write-Host 'Nothing was changed. The SHWeather service stays stopped until you run the installer again.'
    } else {
        Write-Host 'Nothing was installed.'
    }
    exit 1
}

try {
# ---------------------------------------------------------------- copy the app
if ($inPlace) {
    Write-Step "Running from the checkout in $InstallDir (nothing copied)"
} else {
    Write-Step "Copying the app to $InstallDir"
    if (Test-Path -LiteralPath $InstallDir) {
        # Undo the locked-down permissions an earlier installer applied, before overwriting.
        Reset-ShwAcl $InstallDir
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $logDir = Join-Path $DataRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $copyLog = Join-Path $logDir 'install-copy.log'
    # /MIR mirrors the repo; /XD also protects the installed .venv from being purged.
    # /ZB falls back to backup mode (an administrator right) for files it is denied access to.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # A bare name in /XD matches at any depth, so the repo's own data\ and logs\ folders are
    # given as full paths (a bare "data" would also skip web\data, the offline map).
    # The destination's own data\ and logs\ are listed too, so /MIR never purges them.
    $rootOnly = @((Join-Path $Source 'data'), (Join-Path $Source 'logs'),
                  (Join-Path $InstallDir 'data'), (Join-Path $InstallDir 'logs'))
    & robocopy.exe $Source $InstallDir /MIR /ZB /XD $devOnly .venv $rootOnly __pycache__ .pytest_cache .ruff_cache `
        /XF config.yaml '*.pyc' package.json .gitattributes .gitignore /R:2 /W:2 /NP /NDL "/LOG:$copyLog" | Out-Null
    $copyCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($copyCode -ge 8) {
        Write-Host '    robocopy reported:' -ForegroundColor Yellow
        Select-String -LiteralPath $copyLog -Pattern 'ERROR' -Context 0, 1 | Select-Object -First 8 | ForEach-Object {
            Write-Host ('      ' + $_.Line.Trim())
            foreach ($c in $_.Context.PostContext) { if ($c.Trim()) { Write-Host ('        ' + $c.Trim()) } }
        }
        throw "Copying files failed (robocopy exit code $copyCode). Full log: $copyLog"
    }
    # Earlier installers copied these too; /XD keeps /MIR from purging them, so remove them here.
    foreach ($d in $devOnly) {
        $p = Join-Path $InstallDir $d
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
    }
    foreach ($f in 'package.json', '.gitattributes', '.gitignore') {
        $p = Join-Path $InstallDir $f
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    }
}

# ---------------------------------------------------------------- virtualenv
$venvDir = Join-Path $InstallDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    # A virtualenv points at the Python it was made from; if that was removed, rebuild it.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $venvPython -c 'import sys' 2>$null
    $healthy = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    if (-not $healthy) {
        Write-Step 'Rebuilding the virtualenv (the Python it was made from is gone)'
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step 'Creating the Python virtualenv'
    Invoke-Checked $pythonExe @('-m', 'venv', $venvDir) 'Creating the virtualenv'
}
Write-Step 'Installing Python packages (needs internet the first time)'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $venvPython -m pip install --quiet --disable-pip-version-check --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not update pip/setuptools (offline?); continuing with what is installed.' }
$ErrorActionPreference = $prev
Invoke-Checked $venvPython @('-c', 'import setuptools') 'Finding setuptools (the first install needs internet)'
$spec = if ($NoSerial) { $InstallDir } else { "$($InstallDir)[serial]" }
# --no-build-isolation: build with the virtualenv's setuptools, so re-running offline works.
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
    '--no-build-isolation', '-e', $spec) 'Installing SHWeatherService'

# ---------------------------------------------------------------- config and data
Write-Step "Configuration and data in $DataRoot"
foreach ($d in @($DataRoot, (Join-Path $DataRoot 'data'), (Join-Path $DataRoot 'logs'))) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    $example = [System.IO.File]::ReadAllText((Join-Path $InstallDir 'config.example.yaml'))
    $text = New-ShwConfigText $example $Port $InstallDir
    [System.IO.File]::WriteAllText($ConfigPath, $text, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "    created $ConfigPath"
} else {
    Write-Host "    keeping existing $ConfigPath"
    # web_root belongs to the installer: keep it pointing at this install (e.g. after
    # switching between Program Files and running from the git checkout).
    $text = [System.IO.File]::ReadAllText($ConfigPath)
    $webRoot = ($InstallDir.TrimEnd('\') + '\web').Replace('\', '/')
    $updated = Set-ShwConfigLine $text 'web_root' "`"$webRoot`""
    if ($updated -ne $text) { Write-Host "    web_root now points at $webRoot" }
    if ($portGiven -and (Get-ShwConfigPort $ConfigPath 0) -ne $Port) {
        $updated = Set-ShwConfigLine $updated 'port' "$Port"
        Write-Host "    port changed to $Port"
    }
    if ($updated -ne $text) {
        [System.IO.File]::WriteAllText($ConfigPath, $updated, (New-Object System.Text.UTF8Encoding $false))
    }
}

Write-Step 'Setting permissions (config, data and logs editable without admin rights)'
if (-not $inPlace) { Reset-ShwAcl $InstallDir }   # standard Program Files permissions
Reset-ShwAcl $DataRoot -UsersCanEdit      # config.yaml, data\, logs\
} catch {
    if ($wasInstalled) {
        Write-Warning 'The upgrade failed; restarting the previously installed service.'
        Start-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    }
    throw
}

# ---------------------------------------------------------------- firewall
Write-Step 'Opening Windows Defender Firewall'
$profiles = @('Domain', 'Private')
if ($AllowPublicNetworks) { $profiles += 'Public' }
Get-NetFirewallRule -Group $script:FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "SHWeatherService web ($Port/tcp)" -Group $script:FirewallGroup `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile $profiles | Out-Null
foreach ($u in $NmeaUdpPort) {
    New-NetFirewallRule -DisplayName "SHWeatherService NMEA ($u/udp)" -Group $script:FirewallGroup `
        -Direction Inbound -Action Allow -Protocol UDP -LocalPort $u -Profile $profiles | Out-Null
}
Write-Host ("    allowed on {0} networks" -f ($profiles -join '/'))

# ---------------------------------------------------------------- boot-time task
Write-Step 'Registering the background service (scheduled task, runs at boot as SYSTEM)'
$runner = Join-Path $InstallDir 'deploy\windows\run-service.ps1'
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -InstallDir "{1}" -Config "{2}"' -f $runner, $InstallDir, $ConfigPath)
$action = New-ScheduledTaskAction -Execute $psExe -Argument $arguments -WorkingDirectory $DataRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $script:TaskName -Description 'SHWeatherService self-hosted marine weather' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $script:TaskName

# ---------------------------------------------------------------- check
Write-Step "Waiting for the server on port $Port"
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    Start-Sleep -Seconds 2
    $ok = Test-ShwHttp $Port
}
Write-Host ''
if ($ok) {
    Write-Host "SHWeatherService is running. On this PC: http://localhost:$Port" -ForegroundColor Green
    $null = Show-ShwNetworkCheck $Port $InstallDir
} else {
    Write-Warning 'The server did not answer within a minute.'
    if (Show-ShwDiagnosis -Port $Port -Address $bindHost -LogDir (Join-Path $DataRoot 'logs') -Since $started) {
        Write-Host '    Stop that program, or run this installer again with -Port (for example -Port 8090).'
    }
    Write-Host ("    Full logs:  powershell -ExecutionPolicy Bypass -File `"$InstallDir\deploy\windows\shweather-service.ps1`" logs")
}
Write-Host ''
Write-Host "Config:  notepad `"$ConfigPath`"   (set sources.contact, home, sensors; then restart)"
Write-Host "Manage:  powershell -ExecutionPolicy Bypass -File `"$InstallDir\deploy\windows\shweather-service.ps1`" status|start|stop|restart|logs"
Write-Host 'Tip:     stop Windows from sleeping or the service sleeps with it:  powercfg /change standby-timeout-ac 0'
