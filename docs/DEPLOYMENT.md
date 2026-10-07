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
| `-HttpsPort 9443` | Port of the secure address phones need to share their GPS (default 8443; a free one is picked if 8443 is taken). `0` turns HTTPS off. See *Phone GPS and HTTPS* |
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
| Firewall | Inbound rule group **SHWeatherService** for the web and HTTPS ports (and NMEA UDP ports) on Private/Domain networks |

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

Open `http://<hostname>.local:8080` (or the server's IP address; the Windows installer prints them),
or the secure `https://<hostname>.local:8443` to let the phone share its GPS (below). On iOS use
Share → Add to Home Screen; on Android, the browser menu → Install app / Add to Home screen.

### Phone GPS and HTTPS

When the boat has no GPS on its network, a phone or tablet can be the GPS: in the app,
**Settings > Position > Use this device's location**, then pick how often it sends its
position (every 10 seconds to every hour; 1 minute by default). The choice is kept on that
device. While the app is open on screen it reads the device's GPS at that rate and sends
each fix to the server, which uses it for the forecast, tides, buoys and radar.

- A GPS on the boat's network (NMEA, Signal K) always comes first; phones then stand by.
- Fixes rougher than 10 km (a guess from the IP address) are never sent.
- Browsers don't share location from the background, so polling pauses while the app is
  hidden and picks up again as soon as it is back on screen. The server keeps the last
  fix (up to a week) in the meantime.
- The server keeps phone fixes in memory and writes one to the database only when the
  boat has moved about 90 m or every ten minutes, so a phone sending every 10 seconds
  doesn't wear out the Pi's SD card.
- Typing a position in by hand switches that device's location off, so the next fix
  doesn't overwrite it.

**Browsers only give a page the device's location over HTTPS** (or on `localhost`), and
only keep an offline copy of the app (a service worker) there too. So the server also
serves the app over HTTPS, on `https_port` (8443 by default), next to plain HTTP:

```
http://<server>:8080     works everywhere; no phone GPS, no offline copy
https://<server>:8443    phone GPS and offline copy
```

There is no public certificate authority for a boat without a domain name or internet, so
on first start the server makes its own, in `<data_dir>/tls`:

- a small **certificate authority** (CA), valid for 10 years, restricted by *name
  constraints* to private addresses (10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16,
  loopback, IPv6 ULA) and local names (`.local`, `.lan`, `.home.arpa`, `.internal`,
  `localhost`). Even someone who copied its key could not use it to pose as a real website
  to a phone that trusts it;
- a **server certificate** signed by that CA for the machine's local addresses,
  `<hostname>.local` and anything in `tls_names`. It is re-signed by itself when the
  addresses change (a new DHCP lease) and well before it expires (800 days, under Apple's
  limit of 825).

It takes about a second on a PC and a few seconds on a Pi, once; no `openssl` or
`cryptography` package is needed.

On each phone, either:

1. **Accept the warning once.** Open the `https://` address; the browser says the
   connection isn't private because it doesn't know the server's CA. Chrome: *Advanced >
   Proceed*. Safari: *Show Details > visit this website*. The GPS then works; the offline
   copy doesn't (browsers refuse service workers on a certificate they don't trust).
2. **Or install the server's certificate** (no more warnings, offline copy works):
   Settings > Secure connection > *Download the certificate*, then
   - iPhone/iPad: open it in Safari, *Allow*; Settings > *Profile Downloaded* > Install;
     then Settings > General > About > *Certificate Trust Settings* > full trust for
     "SHWeather CA".
   - Android: Settings > search "CA certificate" (Security > Encryption & credentials >
     Install a certificate > CA certificate) and pick `SHWeather-CA.crt`.
   - Windows: open the file > Install Certificate > Local Machine > *Trusted Root
     Certification Authorities*. Mac: Keychain Access > Always Trust. Firefox has its own
     store (Settings > Certificates > Import; use `/api/tls/ca.crt?format=pem`).

   Settings shows the CA's SHA-256 fingerprint to compare with
   `openssl x509 -in <data_dir>/tls/ca.crt -noout -fingerprint -sha256`.

Browsers keep each address's settings apart, so set units, theme and the access token
again on the `https://` address, and re-add the home-screen icon from there.

Settings:

```yaml
https_port: 8443     # 0 = HTTP only
tls_names: [boat.lan, 192.168.8.2]   # extra local names/addresses for the made certificate
tls_cert:            # or your own certificate (PEM, full chain), e.g. from `tailscale cert`
tls_key:             #   ...and its key; then nothing is made and no CA is offered
```

On Windows, `install.ps1` opens the HTTPS port in the firewall too, and picks a free port
if 8443 is taken (`-HttpsPort 9443` chooses one, `-HttpsPort 0` turns HTTPS off).
Deleting `<data_dir>/tls` makes a new CA on the next start; phones that installed the old
one then need the new one.

Other ways to HTTPS still work: Tailscale (`tailscale cert`, then `tls_cert`/`tls_key`),
your own domain with a Let's Encrypt DNS challenge, or a reverse proxy (Caddy, nginx) in
front of port 8080 with `https_port: 0`.

**Key file permissions.** On Linux the keys are readable by the `shweather` user only. On
Windows, `C:\ProgramData\SHWeatherService` is deliberately editable by every local user
(see below), so anyone logged in to the PC can read the CA key. The name constraints limit
what that key can do to impersonating this boat's network.

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
