# Shared helpers for the SHWeatherService Windows scripts. Dot-source it:
#   . (Join-Path $PSScriptRoot 'common.ps1')
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less files as ANSI.

$script:TaskName = 'SHWeatherService'
$script:FirewallGroup = 'SHWeatherService'
$pf = if ($env:ProgramFiles) { $env:ProgramFiles } else { 'C:\Program Files' }
$pd = if ($env:ProgramData) { $env:ProgramData } else { 'C:\ProgramData' }
# String joins (not Join-Path) so these helpers also load under pwsh on Linux for CI checks.
$script:DefaultInstallDir = $pf.TrimEnd('\') + '\SHWeatherService'
$script:DefaultDataRoot = $pd.TrimEnd('\') + '\SHWeatherService'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-ShwConfigPort([string]$ConfigPath, [int]$Default = 8080) {
    try {
        if (Test-Path -LiteralPath $ConfigPath) {
            $text = [System.IO.File]::ReadAllText($ConfigPath)
            $m = [regex]::Match($text, '(?m)^port:\s*(\d+)')
            if ($m.Success) { return [int]$m.Groups[1].Value }
        }
    } catch { }
    return $Default
}

function Reset-ShwAcl([string]$Path, [switch]$UsersCanEdit) {
    # Normal Windows permissions: everything inherits from the parent folder. This also
    # undoes the locked-down permissions the v0.2.0 installer applied. With -UsersCanEdit,
    # every local user may also edit and delete the files (e.g. config.yaml in Notepad,
    # without "Run as administrator"). Never fails the install; only warns.
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & icacls.exe $Path /reset /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not reset permissions on $Path (icacls exit code $LASTEXITCODE)" }
        if ($UsersCanEdit) {
            # BUILTIN\Users by SID (group names are localized): Modify, inherited by everything below
            & icacls.exe $Path /grant '*S-1-5-32-545:(OI)(CI)M' /Q | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Warning "Could not give Users edit rights on $Path (icacls exit code $LASTEXITCODE)" }
        }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Test-ShwCommandLine([string]$CommandLine) {
    # True for the supervisor (run-service.ps1) and the server itself (shweather.exe ... serve,
    # plus the python.exe processes the venv launcher starts). Strict enough not to match
    # editors or shells that merely have the project folder open.
    if (-not $CommandLine) { return $false }
    return (($CommandLine -match 'SHWeatherService' -and $CommandLine -match 'run-service\.ps1') -or
            ($CommandLine -match 'shweather(\.exe)?"?\s.*\bserve\b'))
}

function Get-ShwProcesses {
    Get-CimInstance Win32_Process | Where-Object { Test-ShwCommandLine $_.CommandLine }
}

function Get-ShwConfigHost([string]$ConfigPath) {
    try {
        if (Test-Path -LiteralPath $ConfigPath) {
            $text = [System.IO.File]::ReadAllText($ConfigPath)
            $m = [regex]::Match($text, '(?m)^host:[ \t]*["'']?([^\s"''#]+)')
            if ($m.Success) { return $m.Groups[1].Value }
        }
    } catch { }
    return '0.0.0.0'
}

function Set-ShwConfigLine([string]$Text, [string]$Key, [string]$Value) {
    # Set a top-level "key: value" line in config.yaml text, adding it at the end if missing.
    # [^\r\n]* rather than .* so files saved with Windows line endings keep them.
    $pattern = '(?m)^' + [regex]::Escape($Key) + ':[^\r\n]*'
    $line = $Key + ': ' + $Value
    if ([regex]::IsMatch($Text, $pattern)) { return [regex]::Replace($Text, $pattern, $line.Replace('$', '$$')) }
    $nl = if ($Text -match "`r`n") { "`r`n" } else { "`n" }
    if ($Text -and -not $Text.EndsWith("`n")) { $Text += $nl }
    return $Text + $line + $nl
}

function New-ShwConfigText([string]$ExampleText, [int]$Port, [string]$InstallDir) {
    # config.yaml for a Windows install, derived from config.example.yaml.
    $text = Set-ShwConfigLine $ExampleText 'data_dir' 'data                # relative to this file'
    $text = Set-ShwConfigLine $text 'port' "$Port"
    $webRoot = ($InstallDir.TrimEnd('\') + '\web').Replace('\', '/')
    $text = Set-ShwConfigLine $text 'web_root' "`"$webRoot`""
    $text = Set-ShwConfigLine $text 'log_file' 'logs/shweather.log  # rotated at 5 MB'
    return $text
}

function Get-ShwPortState([int]$Port, [string]$Address = '0.0.0.0') {
    # Can the server listen on this port? Tries it the way the server does (no port sharing):
    #   Free, InUse (another program listens on it), Reserved (Windows refuses it: an excluded
    #   port range kept by Hyper-V/WSL/Docker, or security software) or Unknown.
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$ip)) { $ip = [System.Net.IPAddress]::Any }
    $listener = New-Object System.Net.Sockets.TcpListener -ArgumentList $ip, $Port
    # .NET claims ports exclusively on Windows by default; the server (Python) doesn't, and
    # an exclusive bind can fail where the server's would work (e.g. just after a restart).
    try { $listener.ExclusiveAddressUse = $false } catch { }
    try {
        $listener.Start()
        return 'Free'
    } catch {
        $e = $_.Exception
        while ($e -and -not ($e -is [System.Net.Sockets.SocketException])) { $e = $e.InnerException }
        if ($e -and [string]$e.SocketErrorCode -eq 'AddressAlreadyInUse') { return 'InUse' }
        if ($e -and [string]$e.SocketErrorCode -eq 'AccessDenied') { return 'Reserved' }
        return 'Unknown'
    } finally {
        $listener.Stop()
    }
}

function Wait-ShwPortFree([int]$Port, [string]$Address = '0.0.0.0', [int]$Seconds = 5) {
    # A server that was just stopped can take a moment to let go of its port.
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ($true) {
        $state = Get-ShwPortState $Port $Address
        if ($state -ne 'InUse' -or (Get-Date) -gt $deadline) { return $state }
        Start-Sleep -Milliseconds 500
    }
}

function Find-ShwFreePort([string]$Address = '0.0.0.0', [int[]]$Candidates = @(8090, 8081, 8088, 8000, 8888, 9080)) {
    foreach ($c in $Candidates) {
        if ((Get-ShwPortState $c $Address) -eq 'Free') { return $c }
    }
    return 0
}

function Get-ShwPortOwners([int]$Port) {
    # The programs listening on a TCP port, to name them in messages. Windows only.
    $owners = @()
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return $owners }
    $ids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
             ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    foreach ($id in $ids) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
        $name = 'unknown program'
        $path = ''
        $cmd = ''
        if ($p) { $name = [string]$p.Name; $path = [string]$p.ExecutablePath; $cmd = [string]$p.CommandLine }
        if ($id -eq 4) {
            $name = 'System (HTTP.sys: IIS or another program using the Windows web server; list them with: netsh http show servicestate)'
        }
        $services = @(Get-CimInstance Win32_Service -Filter "ProcessId = $id" -ErrorAction SilentlyContinue |
                      ForEach-Object { [string]$_.DisplayName })
        $owners += [pscustomobject]@{
            ProcessId = $id
            Name      = $name
            Path      = $path
            Services  = $services
            IsShw     = (Test-ShwCommandLine $cmd)
        }
    }
    return $owners
}

function Format-ShwPortOwner($Owner) {
    $line = 'PID {0}  {1}' -f $Owner.ProcessId, $Owner.Name
    if ($Owner.IsShw) { $line += '  (SHWeather)' }
    if (@($Owner.Services).Count -gt 0) { $line += '  (Windows service: ' + (@($Owner.Services) -join ', ') + ')' }
    if ($Owner.Path) { $line += '  ' + $Owner.Path }
    return $line
}

function Format-ShwCommand([string]$Script, [System.Collections.IDictionary]$Params) {
    # The command line to run a script again with these parameters, for copy and paste.
    $parts = @('powershell -ExecutionPolicy Bypass -File', ('"{0}"' -f $Script))
    foreach ($k in $Params.Keys) {
        $v = $Params[$k]
        if ($v -is [System.Management.Automation.SwitchParameter] -or $v -is [bool]) {
            if ([bool]$v) { $parts += "-$k" }
            continue
        }
        $parts += "-$k"
        $s = (@($v) | ForEach-Object { [string]$_ }) -join ','
        if ($s -match '\s' -or $s -eq '') { $s = '"' + $s + '"' }
        $parts += $s
    }
    return ($parts -join ' ')
}

function Get-ShwLogErrors([string]$Path, [datetime]$Since = [datetime]::MinValue, [int]$Count = 3) {
    # The last few ERROR/CRITICAL lines of shweather.log, optionally only those logged since a time.
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $from = $Since.AddTicks(-($Since.Ticks % [TimeSpan]::TicksPerSecond))   # the log has whole seconds
    $found = @(Get-Content -LiteralPath $Path -Tail 400 -ErrorAction SilentlyContinue | Where-Object {
        if (-not ($_ -match '^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\S* (ERROR|CRITICAL) ')) { return $false }
        $when = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
        return ($when -ge $from)
    })
    return @($found | Select-Object -Last $Count)
}

function Show-ShwDiagnosis([int]$Port, [string]$Address, [string]$LogDir, [datetime]$Since = [datetime]::MinValue) {
    # Why is the server not answering? The port first, then what the logs say. Returns $true
    # when another program (or Windows) is in the way on the port.
    $state = Get-ShwPortState $Port $Address
    $owners = @(Get-ShwPortOwners $Port)
    $portProblem = $false
    if ($state -eq 'Reserved') {
        $portProblem = $true
        Write-Host "    Windows does not allow port $Port (a reserved port range, often kept by Hyper-V, WSL or Docker;" -ForegroundColor Yellow
        Write-Host '    list them with: netsh interface ipv4 show excludedportrange protocol=tcp).' -ForegroundColor Yellow
    } elseif ($owners.Count -gt 0 -and -not ($owners | Where-Object { $_.IsShw })) {
        $portProblem = $true
        Write-Host "    Port $Port is in use by another program:" -ForegroundColor Yellow
        $owners | ForEach-Object { Write-Host ('      ' + (Format-ShwPortOwner $_)) }
    } elseif ($owners.Count -gt 0) {
        Write-Host "    Listening on port ${Port}:"
        $owners | ForEach-Object { Write-Host ('      ' + (Format-ShwPortOwner $_)) }
    } elseif ($state -eq 'InUse') {
        Write-Host "    Port $Port is in use (could not tell by which program)." -ForegroundColor Yellow
    }
    $errors = @(Get-ShwLogErrors (Join-Path $LogDir 'shweather.log') $Since)
    if ($errors.Count -gt 0) {
        Write-Host '    Latest errors in shweather.log:' -ForegroundColor Yellow
        $errors | ForEach-Object { Write-Host "      $_" }
    }
    $stderr = Join-Path $LogDir 'stderr.log'
    if ((Test-Path -LiteralPath $stderr) -and (Get-Item -LiteralPath $stderr).Length -gt 0) {
        Write-Host '    stderr.log (crash output):' -ForegroundColor Yellow
        Get-Content -LiteralPath $stderr -Tail 8 | ForEach-Object { Write-Host "      $_" }
    }
    $supervisor = Join-Path $LogDir 'supervisor.log'
    if (Test-Path -LiteralPath $supervisor) {
        $last = Get-Content -LiteralPath $supervisor -Tail 1
        if ($last) { Write-Host "    Supervisor: $last" }
    }
    return $portProblem
}

function Stop-Shw {
    $task = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    }
    # Stopping a task does not always end the processes it started: stop the supervisor
    # first (so it cannot restart the server), then the server.
    $procs = @(Get-ShwProcesses)
    $procs | Where-Object { $_.CommandLine -match 'run-service\.ps1' } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    @(Get-ShwProcesses) | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Test-ShwHttp([int]$Port, [int]$TimeoutSeconds = 3) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/status" -UseBasicParsing -TimeoutSec $TimeoutSeconds
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

# Adapters phones can't reach the server through: virtual switches for WSL, Hyper-V, Docker
# and virtual machines, plus loopback and Bluetooth. Matched against the adapter's name
# and its description (a VirtualBox host-only adapter is just "Ethernet 3" by name).
$script:VirtualAdapters = 'vEthernet|WSL|Hyper-V|Docker|VirtualBox|VMware|Host-Only|Loopback|Bluetooth'

function Get-ShwNetworks {
    # The IPv4 networks this PC is on, the one with the internet/router (default gateway)
    # first: address, adapter, network name and the category Windows gave it (Public,
    # Private or DomainAuthenticated). Empty when this can't be determined.
    $nets = @()
    try {
        $profiles = @{}
        foreach ($p in @(Get-NetConnectionProfile -ErrorAction SilentlyContinue)) { $profiles[[int]$p.InterfaceIndex] = $p }
        $gateways = @{}
        foreach ($r in @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)) { $gateways[[int]$r.InterfaceIndex] = $true }
        $descriptions = @{}
        foreach ($ad in @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue)) {
            $i = $ad.PSObject.Properties['InterfaceIndex']
            if (-not $i) { $i = $ad.PSObject.Properties['ifIndex'] }
            if ($i) { $descriptions[[int]$i.Value] = [string]$ad.InterfaceDescription }
        }
        foreach ($a in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)) {
            $ip = [string]$a.IPAddress
            if ($ip -like '127.*' -or $ip -like '169.254.*') { continue }
            $idx = [int]$a.InterfaceIndex
            if (([string]$a.InterfaceAlias + ' ' + [string]$descriptions[$idx]) -match $script:VirtualAdapters) { continue }
            $p = $profiles[$idx]
            $nets += [pscustomobject]@{
                Address    = $ip
                Adapter    = [string]$a.InterfaceAlias
                Network    = $(if ($p) { [string]$p.Name } else { '' })
                Category   = $(if ($p) { [string]$p.NetworkCategory } else { '' })
                HasGateway = $gateways.ContainsKey($idx)
            }
        }
    } catch { }
    return @($nets | Sort-Object -Property @{ Expression = { -not $_.HasGateway } }, Adapter)
}

function Get-ShwLanUrls([int]$Port) {
    # Addresses to open on a phone. IP addresses, not the PC's name: phones can't look up
    # Windows computer names.
    $urls = @(Get-ShwNetworks | ForEach-Object { "http://$($_.Address):$Port" })
    if ($urls.Count -eq 0) { $urls = @("http://$($env:COMPUTERNAME):$Port") }
    return $urls
}

function Format-ShwTaskResult([int64]$Code) {
    # Task Scheduler's last-run codes in words (they are HRESULTs, e.g. 267009 = 0x41301).
    switch ($Code) {
        0 { return 'finished OK' }
        267009 { return 'running' }           # SCHED_S_TASK_RUNNING
        267011 { return 'has not run yet' }   # SCHED_S_TASK_HAS_NOT_RUN
        267014 { return 'stopped by a user' } # SCHED_S_TASK_TERMINATED
    }
    return ('error 0x{0:X8}' -f ($Code -band [int64]4294967295))
}

function Get-ShwTaskInstallDir([string]$Default) {
    # Where the installed service runs from (Program Files, or a git checkout).
    try {
        $task = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop
        $m = [regex]::Match([string]$task.Actions[0].Arguments, '-InstallDir "([^"]+)"')
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return $Default
}

function Get-ShwPythonPaths([string]$InstallDir) {
    # The interpreters the service runs: the virtualenv's python.exe and the Python it was
    # made from. The latter is the process that listens, so firewall rules for it apply.
    $venv = $InstallDir.TrimEnd('\') + '\.venv'
    $paths = @($venv + '\Scripts\python.exe')
    try {
        $cfg = $venv + '\pyvenv.cfg'
        if (Test-Path -LiteralPath $cfg) {
            $m = [regex]::Match((Get-Content -LiteralPath $cfg -Raw -Encoding UTF8), '(?m)^home\s*=\s*(.+?)\s*$')
            if ($m.Success) { $paths += ($m.Groups[1].Value.TrimEnd('\') + '\python.exe') }
        }
    } catch { }
    return $paths
}

function Get-ShwBlockRules([string[]]$Programs) {
    # Enabled inbound Block rules for these programs. Windows makes them when its "allow
    # Python to communicate on these networks?" prompt is cancelled, or for each network
    # type left unticked. A Block rule beats SHWeather's Allow rule.
    $rules = @()
    foreach ($prog in $Programs) {
        foreach ($spelling in @($prog, $prog.ToLowerInvariant())) {   # prompt-made rules store the path in lower case
            try {
                $rules += @(Get-NetFirewallApplicationFilter -Program $spelling -ErrorAction Stop |
                    Get-NetFirewallRule -ErrorAction SilentlyContinue |
                    Where-Object { [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Block' -and [string]$_.Enabled -eq 'True' })
            } catch { }
        }
    }
    return @($rules | Sort-Object -Property Name -Unique)
}

function Show-ShwNetworkCheck([int]$Port, [string]$InstallDir) {
    # Can phones reach the server? Prints the addresses to use, and anything in the way:
    # a network Windows treats as Public, a missing firewall rule, or a firewall Block rule
    # for the Python the service runs. Returns the number of problems found.
    $problems = 0
    $nets = @(Get-ShwNetworks)
    if ($nets.Count -eq 0) {
        Write-Host ('    Could not list this PC''s networks. Phones use http://<this PC''s IP address>:' + $Port)
    } else {
        Write-Host '    On a phone or tablet on the same network, open:'
        foreach ($n in $nets) {
            $where = $n.Adapter
            if ($n.Network) { $where += ', network "' + $n.Network + '"' }
            if ($n.Category) { $where += ', ' + $n.Category }
            Write-Host ('      http://{0}:{1}   ({2})' -f $n.Address, $Port, $where)
        }
    }

    # SHWeather's own rule: present, enabled, for this port?
    $ourRules = @()
    try {
        $ourRules = @(Get-NetFirewallRule -Group $script:FirewallGroup -ErrorAction Stop)
    } catch {
        if ([string]$_.CategoryInfo.Category -ne 'ObjectNotFound') {
            Write-Host '    Could not read the firewall rules (try again from an Administrator PowerShell to check them).'
            return $problems
        }
    }
    $webRule = $null
    foreach ($r in $ourRules) {
        $pf = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        if ($pf -and [string]$pf.Protocol -eq 'TCP' -and @($pf.LocalPort | ForEach-Object { [string]$_ }) -contains [string]$Port) { $webRule = $r }
    }
    if (-not $webRule) {
        $problems++
        Write-Warning "No firewall rule lets phones reach port $Port (was the port changed in config.yaml by hand?). Run install.ps1 again with -Port $Port to add it."
    } elseif ([string]$webRule.Enabled -ne 'True') {
        $problems++
        Write-Warning "The SHWeatherService firewall rule is turned off. Turn it on: Enable-NetFirewallRule -Group $($script:FirewallGroup)   (Administrator PowerShell)"
    }

    # Networks Windows calls Public, where the rule does not apply (unless -AllowPublicNetworks).
    $allowed = if ($webRule) { [string]$webRule.Profile } else { '' }
    $publicFirewallOn = $true
    try { $publicFirewallOn = ([string](Get-NetFirewallProfile -Name Public -ErrorAction Stop).Enabled -ne 'False') } catch { }
    if ($webRule -and $allowed -notmatch 'Public|Any' -and $publicFirewallOn) {
        foreach ($n in @($nets | Where-Object { $_.Category -eq 'Public' })) {
            $problems++
            $label = if ($n.Network) { '"' + $n.Network + '"' } else { 'on ' + $n.Adapter }
            Write-Warning ("Windows treats the network $label ($($n.Adapter)) as Public, and SHWeather's firewall rule " +
                           "only lets phones in on Private networks, so phones on it are refused.")
            Write-Host '    On your own or the boat''s network, make it Private: Settings > Network & internet >'
            Write-Host "    $($n.Adapter) > (this network's properties) > Network profile type > Private network,"
            Write-Host '    or in an Administrator PowerShell:'
            Write-Host "      Set-NetConnectionProfile -InterfaceAlias '$($n.Adapter)' -NetworkCategory Private"
            Write-Host '    On a network you do not control (marina Wi-Fi), keep it Public: set api_token in config.yaml,'
            Write-Host '    then run install.ps1 again with -AllowPublicNetworks.'
        }
    }

    # Block rules for the service's Python on the networks in use.
    $categories = @($nets | ForEach-Object { if ($_.Category -eq 'DomainAuthenticated') { 'Domain' } else { $_.Category } } |
                    Where-Object { $_ } | Sort-Object -Unique)
    $blocks = @(Get-ShwBlockRules (Get-ShwPythonPaths $InstallDir) | Where-Object {
        $prof = [string]$_.Profile
        ($prof -match 'Any') -or (@($categories | Where-Object { $prof -match $_ }).Count -gt 0)
    })
    if ($blocks.Count -gt 0) {
        $problems++
        Write-Warning ('Windows Firewall has Block rules for the Python that runs SHWeather (made when a Windows Security ' +
                       'Alert about Python was cancelled, or a network type left unticked). Block rules beat allow rules:')
        $blocks | ForEach-Object { Write-Host ('      "{0}" ({1} networks)' -f $_.DisplayName, $_.Profile) }
        Write-Host '    To turn them off, in an Administrator PowerShell:'
        Write-Host ('      Disable-NetFirewallRule -Name ' + (($blocks | ForEach-Object { "'" + $_.Name + "'" }) -join ','))
    }

    # Other firewalls (antivirus suites) have their own rules.
    try {
        $products = @(Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName FirewallProduct -ErrorAction Stop |
                      ForEach-Object { [string]$_.displayName } | Where-Object { $_ })
        if ($products.Count -gt 0) {
            Write-Host "    Note: $($products -join ', ') also filters the network. If phones can't connect, allow TCP port $Port there too."
        }
    } catch { }

    if ($problems -eq 0) {
        Write-Host '    Windows is not blocking phones. If one still cannot connect: type the whole address, with http://;'
        Write-Host '    put the phone on the same Wi-Fi (not a guest network) with any VPN off; on an iPhone use Safari, or'
        Write-Host '    allow your browser Local Network access (iPhone Settings > the browser > Local Network); and check'
        Write-Host '    that the router does not isolate wireless clients ("AP isolation").'
    }
    return $problems
}
