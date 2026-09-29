# Deployment

SHWeather runs on **Raspberry Pi OS / Linux** (systemd service) or **Windows 10/11**
(background service that starts at boot). Both keep working offline; phones and tablets
connect to it over the boat network.

## Linux / Raspberry Pi

### Install

On Raspberry Pi OS (Bookworm or newer) or any Debian-like system:

```bash
git clone https://github.com/TylerNowak/SHWeather.git
cd SHWeather
sudo ./deploy/install.sh [--serial] [--bme280]
```

The script creates a `shweather` system user, installs the code to `/opt/shweather`
with its own virtualenv, writes `/etc/shweather/config.yaml` (only if missing), keeps
data in `/var/lib/shweather`, and enables the `shweather` systemd service.

```bash
sudo nano /etc/shweather/config.yaml   # set sources.contact, home, sensors, bandwidth, display
sudo systemctl restart shweather
journalctl -u shweather -f             # logs
shweather config                        # effective configuration (as any user, with SHWEATHER_CONFIG set)
```

### Update

```bash
cd SHWeather && git pull && sudo ./deploy/install.sh
```

Config and database are preserved.

### Backup

Everything worth keeping is `/etc/shweather/config.yaml` and
`/var/lib/shweather/shweather.db` (forecast cache, instrument history, settings changed
in the app). Copy the database with `sqlite3 shweather.db ".backup backup.db"` while the
service runs.

## Windows 10 / 11

### Prerequisites

- **Python 3.11 or newer, installed for all users** (the service runs as SYSTEM and cannot
  use a per-user Microsoft Store Python):
  `winget install -e --id Python.Python.3.12 --scope machine`, or the python.org installer
  with *Install for all users* ticked.
- **Git** (or download the repository as a ZIP from GitHub).
- An **Administrator** PowerShell.

### Install

```powershell
git clone https://github.com/TylerNowak/SHWeather.git
cd SHWeather
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
```

Options (combine as needed):

| Option | Effect |
|---|---|
| `-Port 8090` | Web/API port (default 8080). On an existing install this moves the server to that port: it updates `config.yaml` and the firewall rule |
| `-NmeaUdpPort 10110` | Also open these UDP ports for broadcasting NMEA multiplexers |
| `-AllowPublicNetworks` | Also accept connections on networks Windows calls *Public*. Windows 11 makes every new network Public until you change it; on your own network, switching it to Private is the better fix (see *Phones can't connect*). Set `api_token` if you do this on marina Wi-Fi. |
| `-NoSerial` | Skip pyserial (USB/COM NMEA inputs) |
| `-Python C:\Python312\python.exe` | Use a specific interpreter |

What it does:

| | Location |
|---|---|
| App + its own virtualenv | `C:\Program Files\SHWeatherService` |
| Config (`config.yaml`), database, logs | `C:\ProgramData\SHWeatherService` (kept on upgrade and uninstall; any local user can edit these without admin rights) |
| Service | Scheduled task **SHWeatherService**, runs at boot as SYSTEM (no login needed), supervised by `run-service.ps1`, which restarts the server if it ever exits |
| Firewall | Inbound rule group **SHWeatherService** for the web port (and NMEA UDP ports) on Private/Domain networks |

The installer first checks that the web port is free. If another program already uses it,
the installer names that program, suggests a free port, and prints the command to run
instead; nothing is installed until the port is free. At the end it waits for the server to
answer, prints the addresses to open on a phone, and warns about anything on the PC that
would stop phones connecting (see *Phones can't connect* below).
Only what the server needs is copied to Program Files (not `.github`, `tests` or the git
history). Changing anything under `C:\Program Files` needs an Administrator PowerShell;
that is how Windows protects every installed program, but you shouldn't need to: all
settings live in `C:\ProgramData\SHWeatherService\config.yaml`.

**Developing on the same PC?** Run the service straight from your git checkout instead of
Program Files; code changes then apply after a `restart`:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -InstallDir D:\Git\SHWeather
```

The virtualenv goes in the checkout's `.venv` (git ignores it), and `uninstall.ps1` never
deletes a folder that contains a git repository.

### Manage

```powershell
$svc = "C:\Program Files\SHWeatherService\deploy\windows\shweather-service.ps1"
powershell -ExecutionPolicy Bypass -File $svc status     # task, web check, phone addresses, firewall check
powershell -ExecutionPolicy Bypass -File $svc restart    # after editing config.yaml (Administrator)
powershell -ExecutionPolicy Bypass -File $svc stop       # (Administrator)
powershell -ExecutionPolicy Bypass -File $svc logs -Tail 100
```

Edit the config with any editor, no "Run as administrator" needed:
`notepad C:\ProgramData\SHWeatherService\config.yaml`, then `restart` (that step needs an
Administrator PowerShell). To change the **port**, run `install.ps1 -Port <new port>` again
instead of editing `port:` by hand: the firewall rule has to move with it, or phones can't
connect.

Logs: `C:\ProgramData\SHWeatherService\logs\shweather.log` (rotated at 5 MB, 3 kept),
`stderr.log` (the last run's crash output, if any) and `supervisor.log` (restarts).

To move or delete files in either folder, `stop` the service first: while it runs it keeps
the database, the log and its own Python files open, and Windows won't delete open files.

> Installed with v0.2.0? That installer locked the folders down so only elevated
> administrators could change them. Re-running the current `install.ps1` restores normal
> permissions, or run these in an Administrator PowerShell:
> `icacls "C:\ProgramData\SHWeatherService" /reset /T /C` then
> `icacls "C:\ProgramData\SHWeatherService" /grant "*S-1-5-32-545:(OI)(CI)M"` and
> `icacls "C:\Program Files\SHWeatherService" /reset /T /C`. The development folders it
> copied can then go: `Remove-Item -Recurse -Force "C:\Program Files\SHWeatherService\.github", "C:\Program Files\SHWeatherService\tests"`.

### Update and uninstall

```powershell
cd SHWeather; git pull
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1      # upgrades in place

powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1    # keeps config + data
powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -RemoveData
```

### Windows specifics

- **Sleep.** A sleeping PC serves nothing. On a boat PC, disable sleep on AC power:
  `powercfg /change standby-timeout-ac 0` (and consider a UPS or a 12 V PC supply).
- **Instruments.** USB-serial adapters and USB GPS pucks appear as COM ports (Device
  Manager > Ports): use `type: serial` with `device: COM3`. OpenCPN on the same PC can
  forward NMEA over TCP/UDP (Options > Connections > add a network output) to a `tcp`
  or `udp` source. Signal K Server also runs on Windows.
- **BME280.** Needs a Linux I2C bus; on Windows use a barometer that reports over NMEA
  (MDA/XDR) or Signal K.
- **Windows Update** restarts are fine: the service starts again at boot.
- **Phones can't connect** (the app works on the PC itself): run `status`; it checks each
  of these and prints the fix.
  - Open the **IP address** it lists (`http://192.168.x.x:8080`), not the PC's name: phones
    can't look up Windows computer names.
  - **The network is Public.** Windows 11 treats every new network as Public until you
    change it, and the firewall rule only covers Private networks. On your own or the
    boat's network: Settings > Network & internet > Wi-Fi (or Ethernet) > the network's
    properties > Network profile type > **Private network**. On marina Wi-Fi keep it Public,
    set `api_token`, and re-run `install.ps1 -AllowPublicNetworks`.
  - **A firewall Block rule for Python.** If a *Windows Security Alert* about Python was ever
    cancelled, or a network type left unticked, Windows added Block rules for that
    `python.exe`, and Block beats Allow. `status` names them and prints the
    `Disable-NetFirewallRule` command.
  - An antivirus suite with its own firewall (Norton, McAfee, Bitdefender...) needs the port
    allowed there as well.
  - On the phone: same Wi-Fi (not a guest network), VPN off, `http://` typed in full. On an
    iPhone, Chrome and other non-Safari browsers need *Local Network* access (iPhone
    Settings > the browser). Some routers isolate wireless clients from each other ("AP
    isolation"); turn that off on the boat's router.
- **"Port 8080 is already in use"**, or the server doesn't answer and `shweather.log` says
  *error while attempting to bind ... only one usage of each socket address* (WinError
  10048): another program (often a development web server, Docker, Jenkins, Tomcat or
  IIS) listens on that port. `status` names it. Either stop that program and keep it from
  starting at boot, or move SHWeather: `install.ps1 -Port 8090`. To see what holds a port
  yourself: `Get-NetTCPConnection -LocalPort 8080 -State Listen | ForEach-Object { Get-Process -Id $_.OwningProcess }`.
  WinError **10013** instead means Windows reserved the port (Hyper-V, WSL and Docker reserve
  ranges: `netsh interface ipv4 show excludedportrange protocol=tcp`); pick a port outside them.
- **"Copying files failed (robocopy exit code 8 or more)"**: the installer prints the files
  robocopy could not copy and why; the full log is
  `C:\ProgramData\SHWeatherService\logs\install-copy.log`. Usual causes: the installer
  was not run from an Administrator PowerShell, or a program (an editor, an Explorer
  preview, antivirus) has one of the files open.
- **Run in the foreground** for testing instead:
  `.venv\Scripts\shweather --config C:\ProgramData\SHWeatherService\config.yaml serve`
  (stop the service first so the port is free).

### Backup

Copy `C:\ProgramData\SHWeatherService` (config.yaml, `data\shweather.db`). Stop the
service first, or use `sqlite3 shweather.db ".backup backup.db"` while it runs.

## Phones and tablets

Open `http://<hostname>.local:8080` (or the server's IP address; the Windows installer prints them). On iOS use Share → Add
to Home Screen; on Android, the browser menu → Install app / Add to Home screen.

### HTTPS and full PWA features

Browsers only allow **service workers** (the phone keeping its own offline copy of the
app) and **the phone's GPS** on secure origins: HTTPS or `localhost`. Over plain
`http://<hostname>.local` the app works normally while the phone can reach the server; it just can't
cache itself on the phone or read the phone's GPS. (The server itself always works offline:
that part never depends on HTTPS.)

Options, simplest first:

1. **Stay on HTTP.** Fine when the server has a GPS or you set the position manually.
2. **Tailscale** (`tailscale cert`) gives the server a real certificate for its
   `*.ts.net` name. Phones need Tailscale installed; it keeps working on the boat LAN.
3. **Your own domain + Let's Encrypt DNS challenge**, with a local DNS record pointing
   to the server, served through Caddy or nginx in front of port 8080. The certificate renews
   whenever the server is online.
4. **A local CA** (e.g. `mkcert`) installed on every phone. Works fully offline, but
   each device must trust the CA.

A built-in HTTPS helper is on the [roadmap](ROADMAP.md).

## Security on shared networks

Reading weather is open to anyone who can reach the server. Endpoints that change things
(boat limits, bandwidth limits, position, downloads) require the app's `X-SHW-Client`
header, which stops other web pages from triggering them, and, if you set `api_token` in
the config, a matching token (enter it once per device under Settings). Set a token
whenever the server is on a network you don't control, such as marina Wi-Fi.

On Windows the config folder is deliberately editable by every local user, for easy
editing on a boat PC. On a PC shared with people you don't trust, tighten the permissions
on `C:\ProgramData\SHWeatherService` yourself: the service runs as SYSTEM and uses
whatever `config.yaml` says.

## Running behind a reverse proxy

SHWeather serves the app at `/` and the API at `/api`. Proxy both unchanged; the
PWA uses relative URLs, so a sub-path such as `/weather/` also works. uvicorn is started
with proxy headers enabled.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
shweather serve --demo          # synthetic data, no network needed
pytest                          # test suite (also runs the Windows script checks if pwsh is installed)
ruff check server tests         # lint
npm test                        # web app unit-system tests (Node 20+)
shweather simulate --udp 127.0.0.1:10110   # and add a udp source on port 10110 to config.yaml
```
