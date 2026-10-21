#!/bin/sh
# Run with sudo only on the KeDei V6.2 Pi, after the temporary SPI test succeeds.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
task_deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python3 - <<'PY'
import datetime
import pathlib
import shutil
import struct

spi = pathlib.Path('/sys/class/spi_master/spi0/device/of_node/cs-gpios')
try:
    value = spi.read_bytes()
except OSError:
    raise SystemExit('First enable and test: sudo dtoverlay spi0-1cs cs0_pin=7')
if len(value) != 12 or struct.unpack('>III', value)[1:] != (7, 1):
    raise SystemExit('SPI0 is not configured for the KeDei test; boot settings were not changed.')

config = pathlib.Path('/boot/firmware/config.txt')
text = config.read_text()
configured = False
for line in text.splitlines():
    active = line.split('#', 1)[0].strip().lower()
    if active.startswith('dtoverlay='):
        name = active.split('=', 1)[1].split(',', 1)[0].strip()
        if name in {'ads7846', 'huga35', 'fbtft', 'waveshare35a', 'waveshare35b'}:
            raise SystemExit(f'Conflicting display overlay {name}; boot settings were not changed.')
        if name == 'spi0-1cs':
            options = {option.strip() for option in active.split(',')[1:]}
            if options != {'cs0_pin=7'}:
                raise SystemExit('Conflicting SPI boot configuration; boot settings were not changed.')
            configured = True

marker = '# wifi-meter KeDei V6.2 shared userspace SPI'
if not configured or marker not in text.splitlines():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = config.with_name('config.txt.wifi-meter-backup-' + stamp)
    shutil.copy2(config, backup)
    addition = '\n\n[all]\n' + marker + '\n'
    if not configured:
        addition += 'dtoverlay=spi0-1cs,cs0_pin=7\n'
    config.write_text(text.rstrip() + addition)
    print(f'Boot configuration backup: {backup}')
else:
    print('KeDei boot configuration already present.')
PY

# Select KeDei explicitly on this Pi even if an HDMI framebuffer appears later.
install -d -m 755 /etc/systemd/system/wifi-meter.service.d
cat > /etc/systemd/system/wifi-meter.service.d/kedei.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -m wifi_meter --backend kedei
UNIT

# Reuse the existing service, scan permissions and current MQTT dependency.
sh "$task_deploy_dir/install.sh"
echo "KeDei display and touch configured for startup; no reboot was required."
