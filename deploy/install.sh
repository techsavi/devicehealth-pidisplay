#!/bin/sh
# Run on the Pi with sudo: installs the polkit scan rule and the systemd service.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

if ! python3 -c "import paho.mqtt" 2>/dev/null; then
    apt-get install -y python3-paho-mqtt
fi

install -m 644 "$here/50-wifi-meter-scan.rules" /etc/polkit-1/rules.d/
install -m 644 "$here/wifi-meter.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable wifi-meter.service
systemctl restart wifi-meter.service
systemctl --no-pager --lines=5 status wifi-meter.service
