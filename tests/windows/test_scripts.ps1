# Checks for the Windows deployment scripts that run on any OS with PowerShell 7 (pwsh):
#   pwsh -NoProfile -File tests/windows/test_scripts.ps1
# Writes the generated config to $env:SHW_TEST_OUT (if set) so pytest can load it.
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$failures = 0
function Check([bool]$Condition, [string]$Name) {
    if ($Condition) { Write-Host "ok   $Name" } else { Write-Host "FAIL $Name"; $script:failures++ }
}

# 1. every script parses
foreach ($f in Get-ChildItem (Join-Path $root 'deploy/windows') -Filter '*.ps1') {
    $tokens = $null; $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors)
    Check ($errors.Count -eq 0) "parses: $($f.Name)"
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    Check (-not ($bytes | Where-Object { $_ -gt 127 })) "ASCII only: $($f.Name)"
}

. (Join-Path $root 'deploy/windows/common.ps1')

# 1b. permissions stay user-friendly: nothing removes inheritance or takes ownership
foreach ($f in Get-ChildItem (Join-Path $root 'deploy/windows') -Filter '*.ps1') {
    $text = [System.IO.File]::ReadAllText($f.FullName)
    Check (-not ($text -match 'inheritance:r|/setowner|Set-Acl')) "no permission lockdown: $($f.Name)"
}
Check ((Get-Command Reset-ShwAcl -ErrorAction SilentlyContinue) -ne $null) 'Reset-ShwAcl available'
$install = [System.IO.File]::ReadAllText((Join-Path $root 'deploy/windows/install.ps1'))
Check ($install -match "devOnly = @\('\.git', '\.github', 'tests'") 'installer leaves dev folders in the repo'
Check ($install -match 'if \(-not \$inPlace\) \{ Reset-ShwAcl \$InstallDir \}') 'in-place install leaves repo permissions alone'
$copyLine = ($install -split "`n" | Where-Object { $_ -match '& robocopy\.exe' })
Check ($copyLine -notmatch '\s(data|logs)\s') 'robocopy: no bare data/logs exclusion (would skip web\data)'
Check ($install -match "rootOnly = @\(\(Join-Path \`$Source 'data'\)") 'robocopy: repo data folder excluded by full path'

# 2. process matching: our server and supervisor, nothing else
$ours = @(
    '"C:\Program Files\SHWeatherService\.venv\Scripts\shweather.exe" --config "C:\ProgramData\SHWeatherService\config.yaml" serve',
    '"C:\Python312\python.exe" "C:\Program Files\SHWeatherService\.venv\Scripts\shweather.exe" --config "C:\ProgramData\SHWeatherService\config.yaml" serve',
    'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Program Files\SHWeatherService\deploy\windows\run-service.ps1" -InstallDir "C:\Program Files\SHWeatherService" -Config "C:\ProgramData\SHWeatherService\config.yaml"',
    'shweather serve --demo'
)
$notOurs = @(
    '"C:\Users\t\AppData\Local\Programs\Microsoft VS Code\Code.exe" D:\Git\SHWeather\server',
    'powershell -ExecutionPolicy Bypass -File "C:\Program Files\SHWeatherService\deploy\windows\shweather-service.ps1" stop',
    'powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1',
    'python -m pytest D:\Git\SHWeather\tests',
    ''
)
foreach ($c in $ours) { Check (Test-ShwCommandLine $c) "matches: $c" }
foreach ($c in $notOurs) { Check (-not (Test-ShwCommandLine $c)) "ignores: $c" }

# 3. generated config
$example = [System.IO.File]::ReadAllText((Join-Path $root 'config.example.yaml'))
$cfg = New-ShwConfigText $example 8090 'C:\Program Files\SHWeatherService'
Check ($cfg -match '(?m)^data_dir: data\b') 'config: data_dir relative'
Check ($cfg -match '(?m)^port: 8090$') 'config: port'
Check ($cfg -match '(?m)^web_root: "C:/Program Files/SHWeatherService/web"') 'config: web_root'
Check ($cfg -match '(?m)^log_file: logs/shweather.log') 'config: log_file'
Check (([regex]::Matches($cfg, '(?m)^(data_dir|port|web_root|log_file):')).Count -eq 4) 'config: no duplicate keys'
if ($env:SHW_TEST_OUT) { [System.IO.File]::WriteAllText($env:SHW_TEST_OUT, $cfg, (New-Object System.Text.UTF8Encoding $false)) }

# 4. port lookup
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, "host: 0.0.0.0`nport: 9123`n")
Check ((Get-ShwConfigPort $tmp) -eq 9123) 'port read from config'
Check ((Get-ShwConfigPort '/nonexistent/config.yaml' 8080) -eq 8080) 'port default'
Remove-Item $tmp

# 5. config lines: replaced in place keeping Windows line endings, added when missing
$t = Set-ShwConfigLine "host: 0.0.0.0`r`nport: 8080`r`nboat:`r`n  name: x`r`n" 'port' '8090'
Check ($t -eq "host: 0.0.0.0`r`nport: 8090`r`nboat:`r`n  name: x`r`n") 'config line: replaced, CRLF kept'
$t = Set-ShwConfigLine "host: 0.0.0.0`nsensors:`n  port: 10110" 'port' '8090'
Check ($t -eq "host: 0.0.0.0`nsensors:`n  port: 10110`nport: 8090`n") 'config line: added at top level when missing'
$t = Set-ShwConfigLine "web_root: old`n" 'web_root' '"C:/Users/$me/web"'
Check ($t -eq "web_root: `"C:/Users/`$me/web`"`n") 'config line: $ in value kept'
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tmp, "host: `"192.168.1.5`"  # boat LAN only`nport: 9123`n")
Check ((Get-ShwConfigHost $tmp) -eq '192.168.1.5') 'host read from config'
Check ((Get-ShwConfigHost '/nonexistent/config.yaml') -eq '0.0.0.0') 'host default'
Remove-Item $tmp

# 6. port checks: another program on the port is detected, and a free port suggested
$other = New-Object System.Net.Sockets.TcpListener -ArgumentList ([System.Net.IPAddress]::Loopback), 0
$other.Start()
$busy = $other.LocalEndpoint.Port
$probe = New-Object System.Net.Sockets.TcpListener -ArgumentList ([System.Net.IPAddress]::Loopback), 0
$probe.Start(); $spare = $probe.LocalEndpoint.Port; $probe.Stop()
Check ((Get-ShwPortState $busy '127.0.0.1') -eq 'InUse') 'port in use detected'
Check ((Wait-ShwPortFree $busy '127.0.0.1' 1) -eq 'InUse') 'waiting gives up on a port another program keeps'
Check ((Find-ShwFreePort '127.0.0.1' @($busy, $spare)) -eq $spare) 'free port suggested'
if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $owners = @(Get-ShwPortOwners $busy)
    Check (($owners | Where-Object { $_.ProcessId -eq $PID }).Count -eq 1) 'port owner named'
    Check ((Format-ShwPortOwner $owners[0]) -match "^PID $PID ") 'port owner formatted'
} else {
    Check (@(Get-ShwPortOwners $busy).Count -eq 0) 'port owners: none without Get-NetTCPConnection'
}
$other.Stop()
Check ((Get-ShwPortState $busy '127.0.0.1') -eq 'Free') 'port free once released'
Check ((Find-ShwFreePort '127.0.0.1' @()) -eq 0) 'no candidates, no port'
$fake = [pscustomobject]@{ ProcessId = 4321; Name = 'httpd.exe'; Path = 'C:\Apache24\bin\httpd.exe'; Services = @('Apache2.4'); IsShw = $false }
Check ((Format-ShwPortOwner $fake) -eq 'PID 4321  httpd.exe  (Windows service: Apache2.4)  C:\Apache24\bin\httpd.exe') 'port owner line'

# 7. the command to re-run the installer keeps the options that were used
$params = [ordered]@{
    NmeaUdpPort = [int[]]@(10110, 2000); AllowPublicNetworks = [switch]$true; NoSerial = [switch]$false
    Python = 'C:\Program Files\Python312\python.exe'; Port = 8090
}
$cmd = Format-ShwCommand 'C:\src\deploy\windows\install.ps1' $params
Check ($cmd -eq 'powershell -ExecutionPolicy Bypass -File "C:\src\deploy\windows\install.ps1" -NmeaUdpPort 10110,2000 -AllowPublicNetworks -Python "C:\Program Files\Python312\python.exe" -Port 8090') 're-run command'
Check ($install -match "PSBoundParameters\.ContainsKey\('Port'\)") 'installer: -Port updates an existing install'
Check ($install.IndexOf('Checking that port') -lt $install.IndexOf('Copying the app')) 'installer: port checked before copying'

# 8. recent errors from the server log
$log = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllLines($log, [string[]]@(
    '2026-09-28 13:11:57,222 ERROR uvicorn.error: [Errno 10048] error while attempting to bind',
    '2026-09-28 14:00:00,000 INFO shweather.app: started',
    '2026-09-28 14:00:01,500 ERROR shweather: Port 8080 is already in use by another program',
    'Traceback (most recent call last):'))
$recent = @(Get-ShwLogErrors $log ([datetime]'2026-09-28 14:00:00'))
Check ($recent.Count -eq 1 -and $recent[0] -match 'already in use') 'log errors since the install'
Check (@(Get-ShwLogErrors $log).Count -eq 2) 'log errors, all'
Check (@(Get-ShwLogErrors '/nonexistent/shweather.log').Count -eq 0) 'log errors: no log yet'
Remove-Item $log

# 9. can phones reach it? Windows' networking and firewall cmdlets are mocked here.
$inst = Join-Path ([System.IO.Path]::GetTempPath()) ('shw-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path (Join-Path $inst '.venv') | Out-Null
[System.IO.File]::WriteAllText((Join-Path $inst '.venv/pyvenv.cfg'),
    "home = C:\Users\killc\AppData\Local\Programs\Python\Python313`r`ninclude-system-site-packages = false`r`n")
$py = @(Get-ShwPythonPaths $inst)
Check ($py.Count -eq 2 -and $py[1] -eq 'C:\Users\killc\AppData\Local\Programs\Python\Python313\python.exe') 'service Python found from pyvenv.cfg'

function global:Get-NetConnectionProfile { [CmdletBinding()] param() $global:ShwMock.Profiles }
function global:Get-NetRoute { [CmdletBinding()] param($DestinationPrefix) $global:ShwMock.Routes }
function global:Get-NetIPAddress { [CmdletBinding()] param($AddressFamily) $global:ShwMock.Addresses }
function global:Get-NetAdapter { [CmdletBinding()] param([switch]$IncludeHidden) $global:ShwMock.Adapters }
function global:Get-NetFirewallProfile { [CmdletBinding()] param($Name) [pscustomobject]@{ Enabled = 'True' } }
function global:Get-NetFirewallPortFilter {
    [CmdletBinding()] param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process { $InputObject.PortFilter }
}
function global:Get-NetFirewallApplicationFilter {
    [CmdletBinding()] param([string]$Program)
    # rules made by the Windows prompt store the path in lower case; match exactly, as a worst case
    $hits = @($global:ShwMock.Blocks | Where-Object { $_.Program -ceq $Program })
    if ($hits.Count -eq 0) { Write-Error "No MSFT_NetApplicationFilter objects found with AppPath $Program"; return }
    $hits | ForEach-Object { [pscustomobject]@{ Program = $_.Program } }
}
function global:Get-NetFirewallRule {
    [CmdletBinding()] param($Group, [Parameter(ValueFromPipeline = $true)]$InputObject)
    process {
        if ($global:ShwMock['Denied']) { Write-Error -Message 'Access is denied.' -Category PermissionDenied; return }
        if ($InputObject) { $global:ShwMock.Blocks | Where-Object { $_.Program -eq $InputObject.Program } }
        elseif ($Group) { $global:ShwMock.Rules }
    }
}
function global:Get-CimInstance { [CmdletBinding()] param($Namespace, $ClassName, $Filter) $global:ShwMock.Products }
function global:Get-ScheduledTask { [CmdletBinding()] param($TaskName)
    [pscustomobject]@{ Actions = @([pscustomobject]@{ Arguments = '-NoProfile -File "C:\Program Files\SHWeatherService\deploy\windows\run-service.ps1" -InstallDir "C:\Program Files\SHWeatherService" -Config "C:\ProgramData\SHWeatherService\config.yaml"' }) }
}
function New-MockRule([string]$Port, [string]$Profile, [string]$Enabled = 'True') {
    [pscustomobject]@{ Name = 'shw-web'; DisplayName = "SHWeatherService web ($Port/tcp)"; Enabled = $Enabled; Profile = $Profile
                       Direction = 'Inbound'; Action = 'Allow'; PortFilter = [pscustomobject]@{ Protocol = 'TCP'; LocalPort = $Port } }
}
function Invoke-NetworkCheck {
    Set-StrictMode -Version 3   # as in install.ps1
    $items = @(Show-ShwNetworkCheck 8090 $inst 6>&1 3>&1)
    return [pscustomobject]@{
        Problems = @($items | Where-Object { $_ -is [int] })[-1]
        Text     = (@($items | Where-Object { $_ -isnot [int] } | ForEach-Object { "$_" }) -join "`n")
    }
}
$pyLower = 'c:\users\killc\appdata\local\programs\python\python313\python.exe'

# 9a. home Wi-Fi left as Public, and a Block rule from a dismissed Python firewall prompt
$global:ShwMock = @{
    Addresses = @([pscustomobject]@{ IPAddress = '172.20.16.1'; InterfaceAlias = 'vEthernet (WSL)'; InterfaceIndex = 40 },
                  [pscustomobject]@{ IPAddress = '192.168.1.50'; InterfaceAlias = 'Wi-Fi'; InterfaceIndex = 12 },
                  [pscustomobject]@{ IPAddress = '192.168.56.1'; InterfaceAlias = 'Ethernet 3'; InterfaceIndex = 20 },
                  [pscustomobject]@{ IPAddress = '127.0.0.1'; InterfaceAlias = 'Loopback Pseudo-Interface 1'; InterfaceIndex = 1 })
    Adapters  = @([pscustomobject]@{ ifIndex = 20; InterfaceDescription = 'VirtualBox Host-Only Ethernet Adapter' },
                  [pscustomobject]@{ InterfaceIndex = 12; InterfaceDescription = 'Intel(R) Wi-Fi 6 AX201 160MHz' })
    Profiles  = @([pscustomobject]@{ InterfaceIndex = 12; Name = 'HomeNet'; NetworkCategory = 'Public' },
                  [pscustomobject]@{ InterfaceIndex = 40; Name = 'Unidentified network'; NetworkCategory = 'Public' })
    Routes    = @([pscustomobject]@{ InterfaceIndex = 12 })
    Rules     = @(New-MockRule '8090' 'Domain, Private')
    Blocks    = @([pscustomobject]@{ Name = "TCP Query User{0A1B}$pyLower"; DisplayName = 'python.exe'; Program = $pyLower
                                     Profile = 'Public'; Direction = 'Inbound'; Action = 'Block'; Enabled = 'True' })
    Products  = @()
}
$r = Invoke-NetworkCheck
Check ($r.Text -match 'http://192\.168\.1\.50:8090' -and $r.Text -notmatch '172\.20\.16\.1|192\.168\.56\.1') 'phone URL: LAN address, not WSL/VirtualBox/loopback'
Check ($r.Text -match 'network "HomeNet" \(Wi-Fi\) as Public') 'Public network flagged'
Check ($r.Text -match "Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Private") 'Public network: fix command'
Check ($r.Text -match 'Disable-NetFirewallRule -Name ''TCP Query User\{0A1B\}c:\\users') 'Python Block rule flagged with fix'
Check ($r.Problems -eq 2) 'two problems counted'
$urls = @(Get-ShwLanUrls 8090)
Check ($urls.Count -eq 1 -and $urls[0] -eq 'http://192.168.1.50:8090') 'Get-ShwLanUrls: IP addresses only'
Check ((Get-ShwTaskInstallDir 'D:\x') -eq 'C:\Program Files\SHWeatherService') 'install folder read from the task'

# 9b. Private network, no Block rules: nothing in the way
$global:ShwMock.Profiles = @([pscustomobject]@{ InterfaceIndex = 12; Name = 'HomeNet'; NetworkCategory = 'Private' })
$r = Invoke-NetworkCheck
Check ($r.Problems -eq 0 -and $r.Text -match 'Windows is not blocking phones') 'Private network: all clear'

# 9c. -AllowPublicNetworks: the rule covers Public networks too
$global:ShwMock.Profiles = @([pscustomobject]@{ InterfaceIndex = 12; Name = 'HomeNet'; NetworkCategory = 'Public' })
$global:ShwMock.Rules = @(New-MockRule '8090' 'Any')
$r = Invoke-NetworkCheck
Check ($r.Text -notmatch 'as Public' -and $r.Problems -eq 1) 'rule for all networks: only the Block rule remains'

# 9d. port changed by hand in config.yaml: the firewall rule is for the old port
$global:ShwMock.Blocks = @()
$global:ShwMock.Rules = @(New-MockRule '8080' 'Domain, Private')
$r = Invoke-NetworkCheck
Check ($r.Text -match 'No firewall rule lets phones reach port 8090' -and $r.Problems -eq 1) 'firewall rule for another port flagged'
$global:ShwMock.Rules = @(New-MockRule '8090' 'Domain, Private' 'False')
$r = Invoke-NetworkCheck
Check ($r.Text -match 'firewall rule is turned off') 'disabled firewall rule flagged'
$global:ShwMock.Denied = $true
$r = Invoke-NetworkCheck
Check ($r.Text -match 'Could not read the firewall rules' -and $r.Text -notmatch 'No firewall rule' -and $r.Problems -eq 0) 'unreadable firewall: says so, no false alarm'
$global:ShwMock.Denied = $false

# 9e. an antivirus firewall is mentioned; no networks at all falls back to the PC name
$global:ShwMock.Rules = @(New-MockRule '8090' 'Domain, Private')
$global:ShwMock.Profiles = @([pscustomobject]@{ InterfaceIndex = 12; Name = 'HomeNet'; NetworkCategory = 'Private' })
$global:ShwMock.Products = @([pscustomobject]@{ displayName = 'Norton Firewall' })
$r = Invoke-NetworkCheck
Check ($r.Text -match 'Norton Firewall also filters the network') 'third-party firewall mentioned'
$global:ShwMock.Addresses = @()
Check (@(Get-ShwLanUrls 8090)[0] -match '^http://.*:8090$') 'no networks: falls back to the PC name'
$r = Invoke-NetworkCheck
Check ($r.Text -match 'Could not list') 'no networks: says so'
Check ((Format-ShwTaskResult 267009) -eq 'running') 'task result: running'
Check ((Format-ShwTaskResult 0) -eq 'finished OK') 'task result: OK'
Check ((Format-ShwTaskResult -2147024894) -eq 'error 0x80070002') 'task result: error code in hex'
foreach ($f in 'Get-NetAdapter', 'Get-NetConnectionProfile', 'Get-NetRoute', 'Get-NetIPAddress', 'Get-NetFirewallProfile', 'Get-NetFirewallPortFilter',
               'Get-NetFirewallApplicationFilter', 'Get-NetFirewallRule', 'Get-CimInstance', 'Get-ScheduledTask') {
    Remove-Item "function:global:$f" -ErrorAction SilentlyContinue
}
Remove-Item -Recurse -Force $inst

if ($failures) { Write-Host "$failures check(s) failed"; exit 1 }
Write-Host 'all Windows script checks passed'
