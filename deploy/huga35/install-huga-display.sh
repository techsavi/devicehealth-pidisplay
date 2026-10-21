#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run: sudo /home/pi/install-huga-display.sh"
    exit 1
fi
cp -a /boot/firmware/config.txt /boot/firmware/config.txt.before-huga
install -m 644 /home/pi/huga35.dtbo /boot/firmware/overlays/huga35.dtbo
python3 - <<'PY'
from pathlib import Path
p = Path("/boot/firmware/config.txt")
s = p.read_text()
if "dtoverlay=huga35" not in s:
    s += "\n[all]\n# HUGA 3.5 inch V3.0 ILI9486 LCD with XPT2046 touch\ndtparam=spi=on\ndtoverlay=huga35,rotate=90,speed=16000000,fps=15\n"
p.write_text(s)
PY
printf "Installed HUGA LCD overlay. Reboot to activate it.\n"
