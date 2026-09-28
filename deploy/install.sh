#!/usr/bin/env bash
# Install SHWeatherService on Raspberry Pi OS (Bookworm or newer) or any Debian-like system.
#
#   sudo ./deploy/install.sh                  # core
#   sudo ./deploy/install.sh --serial         # + NMEA 0183 over USB/RS-422
#   sudo ./deploy/install.sh --bme280         # + BME280 barometer on I2C
#
# Re-running it upgrades the code and keeps /etc/shweather/config.yaml and the database.
set -euo pipefail

PREFIX=/opt/shweather
CONF_DIR=/etc/shweather
DATA_DIR=/var/lib/shweather
EXTRAS=()

for arg in "$@"; do
  case "$arg" in
    --serial) EXTRAS+=(serial) ;;
    --bme280) EXTRAS+=(bme280) ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo." >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> Installing from $SRC"

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync >/dev/null
if [[ " ${EXTRAS[*]} " == *" bme280 "* ]]; then
  apt-get install -y -qq i2c-tools >/dev/null
  command -v raspi-config >/dev/null && raspi-config nonint do_i2c 0 || true
fi

id shweather >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin shweather
for g in dialout i2c; do getent group "$g" >/dev/null && usermod -aG "$g" shweather; done

echo "==> Copying code to $PREFIX"
mkdir -p "$PREFIX"
# Leading "/" anchors an exclude to the repository root: a bare "data" would also drop web/data.
rsync -a --delete --exclude /.git --exclude /.venv --exclude /data --exclude '__pycache__' \
  --exclude config.yaml "$SRC"/ "$PREFIX"/

echo "==> Python environment"
[[ -d "$PREFIX/.venv" ]] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --upgrade pip setuptools wheel || echo "    (could not update pip/setuptools; continuing offline)"
spec="$PREFIX"
if ((${#EXTRAS[@]})); then spec="$PREFIX[$(IFS=,; echo "${EXTRAS[*]}")]"; fi
# --no-build-isolation: build with the venv's setuptools so re-running offline works
"$PREFIX/.venv/bin/pip" install -q --no-build-isolation -e "$spec"

echo "==> Configuration"
mkdir -p "$CONF_DIR" "$DATA_DIR"
chown shweather:shweather "$DATA_DIR"
if [[ ! -f "$CONF_DIR/config.yaml" ]]; then
  sed -e "s#^data_dir: .*#data_dir: $DATA_DIR#" -e "s#^web_root: .*#web_root: $PREFIX/web#" \
    "$PREFIX/config.example.yaml" > "$CONF_DIR/config.yaml"
  echo "    Created $CONF_DIR/config.yaml - edit it (contact, position, instruments)."
else
  echo "    Keeping existing $CONF_DIR/config.yaml"
fi

echo "==> systemd service"
install -m 0644 "$PREFIX/deploy/shweather.service" /etc/systemd/system/shweather.service
systemctl daemon-reload
systemctl enable --now shweather.service
sleep 2
systemctl --no-pager --lines=5 status shweather.service || true

host="$(hostname).local"
port="$(awk '/^port:/ {print $2}' "$CONF_DIR/config.yaml" 2>/dev/null)"
echo
echo "Done. Open http://$host:${port:-8080} on a phone or tablet on the boat network."
echo "Logs: journalctl -u shweather -f     Config: $CONF_DIR/config.yaml"
