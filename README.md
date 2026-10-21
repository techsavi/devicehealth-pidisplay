# wifi-meter

Two views for a Raspberry Pi with a HUGA 3.5" 480x320 ILI9486 LCD (`/dev/fb0`, RGB565).
Swipe left or right to switch between them. The dots after the title show which view
is up, and the choice is saved in `~/.config/wifi-meter/view`.

It also supports the KeDei 3.5-inch SPI TFT LCD V6.2 (2016/6/1) (see
[KeDei V6.2](#kedei-v62)). The app uses an existing framebuffer, or the configured KeDei
backend when there is none.

**Wi-Fi Signal**: nearby networks, one per line: SSID (truncated with … if long; the
connected network is highlighted with ●), a 10-segment signal bar, channel, and the
NetworkManager signal percentage. Ten rows fit on screen, strongest first. Access points
sharing an SSID are merged into one row; pass `--all-aps` to list each.

**Server Status**: the webserver's "Server status" tile (`../webserver` services page),
fed over MQTT. `../statuspanel`'s poller publishes the tile's data, its `/api/summary`,
as retained `devices/statuspanel/summary` after every poll (every 5 min). The view
shows the same things the tile does:
- a healthy / warning / critical / stale pill, or "unavailable" when StatusPanel is stopped;
- host · updated N ago · site up / site down;
- CPU, Memory, Disk /, Certs and Updates, amber from 80 % and red from 90 % (certs amber
  under 14 days);
- the top three issues with their age, and "+N more";
- in place of the tile's supervisor row, StatusPanel's own state from
  `status/statuspanel` and its uptime.

## Touch controls

| Touch | Action |
|---|---|
| Swipe left / right (at least 100 px across) | next / previous view |
| ▲ (upper half of the right edge, Wi-Fi view) | page up |
| ▼ (lower half of the right edge, Wi-Fi view) | page down |
| Tap or swipe on an alert card | dismiss it on this screen |
| Press and hold ~1 s anywhere else, then release | rotate the display 180° |

The touch layer reads compressed near the panel edges: everything in the top ~40 px
(the title bar) registers at about y=42, so the title bar can't be a tap target.

The scroll indicator between ▲ and ▼ shows which part of the list is on screen; an arrow
is greyed out when there is nothing further that way. The rotation is saved in
`~/.config/wifi-meter/flipped` and survives restarts.

The meter grabs the ADS7846 touchscreen exclusively while running, so taps don't also
click on the HDMI desktop. Pass `--no-touch` to leave the touchscreen alone.

### Calibration

The resistive panel reports raw 0–4095 readings with its axes swapped relative to the
rotated display. Defaults measured on this panel are built in (`touch.Calibration`); to
recalibrate, tap the four crosshairs, then tap around to check the dots land under your
finger:

```sh
sudo systemctl stop wifi-meter
cd ~/pi_display && python3 -m wifi_meter.calibrate     # saves ~/.config/wifi-meter/touch.json
sudo systemctl start wifi-meter
```

## MQTT alerts (LAN only)

The meter joins the alert bus run by the sister project `../webserver` (Mosquitto on
the PC that runs it, port 1883) **as the device itself**: the name is the Pi's hostname, read at
startup, so a renamed Pi is picked up automatically. The login is the device's shared one
from the pi-health agent (`~/.config/pi-health/config.json`, written by
`../webserver/agents/pi/deploy.mjs`), so the Pi shows up as one device on the bus:

| Client id | Program |
|---|---|
| `<hostname>-health` | pi-health agent: `status/<hostname>`, `devices/<hostname>/health`, its own alerts |
| `<hostname>-display` | this meter |
| `<hostname>-notify` | `python3 -m wifi_meter.notify` (connects only to send) |

| Topic | Direction | Use |
|---|---|---|
| `alerts/+/+` | receive | retained alerts from every source, shown as a card over the list |
| `devices/<hostname>/display` | send | retained `online`; last will `offline` if the meter drops |
| `devices/<hostname>/wifi` | send | retained `{ssid, signal, channel, networks, since}` after each scan |
| `alerts/<hostname>/<id>` | send | alerts raised from the Pi with `python3 -m wifi_meter.notify` |

`status/<hostname>` belongs to pi-health. Two programs can't share one last-will topic
without one marking the whole Pi offline when the other stops.

The broker only lets a login write under its own name. After renaming the Pi, run
`node agents/pi/deploy.mjs <ssh host> <new name>` in `../webserver` to create the new
login. Until then the meter logs a warning that the login and hostname don't match.

On screen, the dot after the title is green while connected to the bus. An alert card
covers the list (critical first, then newest); tap it to dismiss it on this screen, and
the next one shows. Dismissed alerts come back if the source re-raises them or the meter
restarts. Alerts with `expires` disappear on their own, and an empty retained message
clears one. Like TabletPanel, the meter shows warning and critical alerts from every
source plus all levels from `manual`, `tabletpanel` and this device.

Sending from the Pi:

```sh
python3 -m wifi_meter.notify "Door open" "Garage door open 10 min" --level warning
python3 -m wifi_meter.notify "Back soon" --expires 600       # clears itself after 10 min
python3 -m wifi_meter.notify --id door-open --clear
```

**LAN only:** the client resolves the broker host and refuses to connect unless the
address is private (RFC 1918 or loopback) and not on the tailnet (100.64.0.0/10). On the
PC, the Windows firewall rule from `../webserver` limits 1883 to the local subnet and
tailnet. The connection is unencrypted, which is fine on the home LAN.

**Setup**:

1. Deploy the pi-health agent from the PC: `node agents/pi/deploy.mjs <ssh host>` in
   `../webserver`. This creates the device login and writes `~/.config/pi-health/config.json`.
2. `.\deploy.ps1 -Install` installs `python3-paho-mqtt`.
3. Optionally, set meter-only settings in `~/.config/wifi-meter/mqtt.json`:
   `min_level` (default `"warning"`) and `all_levels_from` (sources whose info alerts are
   also shown).

Without the device login or the library, the meter runs without MQTT. `--no-mqtt` turns
it off.

## KeDei V6.2

The V6.2 uses GPIO7 for LCD chip select and GPIO8 for both the LCD latch and touch
chip select; touch PENIRQ is GPIO25. It needs its exact V6.2 framing and register
initialization. A generic Waveshare/ILI9486 overlay or the V6.3 fbcp driver does not
provide that transport.

Configure the following under `[all]` in `/boot/firmware/config.txt`:

```ini
dtoverlay=spi0-1cs,cs0_pin=7
```

This maps logical `/dev/spidev0.0` to physical GPIO7 and leaves GPIO8 free for the
app. For a temporary test without rebooting, with no other SPI0 overlay active:

```sh
sudo dtoverlay spi0-1cs cs0_pin=7
```

The app owns LCD and touch together. Do not enable the native ADS7846 overlay in
this configuration: although native ADS7846 touch was verified on the current
6.18 kernel, it claims the shared GPIO8 line and disables the spidev devices.

The native helper (`wifi_meter/_kedei_transport.c`) builds automatically on the Pi
with `cc` the first time it is needed, and again whenever the source is newer than the
built `.so` (which is not in git). It uses GPIO character-device v2, SPI ioctls, and
one shared lock across display and touch operations; it requires membership in the
`gpio` and `spi` groups. There is no vendor kernel, replacement framebuffer, WiringPi
or graphics-stack downgrade. The conservative default LCD clock is 4 MHz, and touch
reads use 500 kHz.

```sh
cd ~/pi_display
python3 -m wifi_meter.kedei                 # test card
python3 -m wifi_meter.kedei --touch 90      # test card, then print taps for 90 s
python3 -m wifi_meter.calibrate --backend kedei
python3 -m wifi_meter --backend kedei --no-mqtt
```

After calibration and the live meter test succeed, persist the SPI mapping and
install/start the existing meter service on this KeDei Pi:

```sh
sudo sh ~/pi_display/deploy/install-kedei.sh
```

The installer backs up `config.txt`, preserves the current kernel and graphics
settings, and selects KeDei explicitly for this Pi's service (a drop-in running
`--backend kedei`). It installs the existing scan permission rule and MQTT dependency.
The HUGA installer and display assets in `deploy/huga35/` remain available for that
display.

The built-in calibration defaults were measured on a HUGA display. Calibrate the KeDei
panel before relying on swipes, scrolling and long-press rotation. An explicit
`--backend framebuffer` keeps the original framebuffer path; preview commands render
PNGs without opening display hardware.

Backend selection tests (no hardware needed):

```sh
python3 -m unittest discover -s tests -v
```

## Orientation sensing

### Hardware options for automatic detection

The Pi 3 has no built-in accelerometer and none is fitted (no IIO devices, no HAT, the
ARM I2C bus is disabled). To flip automatically when the device is turned over, add a
sensor on the free side of the header. The display only uses GPIO 7–11 (SPI0), 17 (touch
IRQ), 24 (DC) and 25 (reset), all on pins 1–26, so pins 27–40 are free. Its connector
physically covers pins 3/5, the normal I2C bus.

| Option | Cost | Wiring | Notes |
|---|---|---|---|
| Ball tilt switch (e.g. SW-520D) | ~$1 | GPIO 26 (pin 37) + GND (pin 39), internal pull-up | Simplest. Binary upright/inverted signal; needs ~1 s debounce since the ball chatters near horizontal. |
| I2C accelerometer (MPU-6050, ADXL345, LIS3DH) | ~$3–5 | Software I2C on GPIO 5/6 (pins 29/31), 3.3 V (pin 17), GND (pin 39) | Reports the actual tilt angle, so flipping can use hysteresis (e.g. flip past 30° beyond horizontal) and never flickers. Enable with `dtoverlay=i2c-gpio,i2c_gpio_sda=5,i2c_gpio_scl=6` in `/boot/firmware/config.txt`; the sensor then appears on a new `/dev/i2c-*` bus (or as an IIO device if its kernel driver overlay is also loaded). |

Either would call the same `Orientation` flip used by tap-to-flip, with the tap kept as a
manual override.

## Layout

```
wifi_meter/   Python package (python3 -m wifi_meter)
  scanner.py  nmcli scan + parsing
  render.py   Pillow drawing of the Wi-Fi view, header, alert and identify screens
  server_view.py  the Server Status view (devices/statuspanel/summary)
  display.py  RGB565 framebuffer writer
  kedei.py    KeDei 3.5" V6.2 display + XPT2046 touch backend
  _kedei_transport.c  native SPI/GPIO helper for kedei.py, built on the Pi
  touch.py    raw evdev tap reading + calibration for the ADS7846 touchscreen
  calibrate.py  python3 -m wifi_meter.calibrate
  bus.py      MQTT alert bus client (paho-mqtt)
  notify.py   python3 -m wifi_meter.notify
tests/        backend selection and redraw tests
deploy/       systemd unit, polkit rule, install.sh (run on the Pi)
deploy.ps1    copy to the Pi from Windows and restart
deploy.local.example.json   copy to deploy.local.json (gitignored) and set your Pi
```

Pi requirements (already present on Raspberry Pi OS): `python3-pil`, `python3-numpy`,
NetworkManager, and the `huga35` display overlay.

## Deploy

Copy `deploy.local.example.json` to `deploy.local.json` and set `target` to your Pi's ssh
host. That file is gitignored, so local host names stay out of the repo.

```powershell
.\deploy.ps1 -Install   # first time: copies code, installs service + polkit rule (asks for sudo password)
.\deploy.ps1            # later updates: copies code and restarts the service
.\deploy.ps1 -Target otherpi   # another Pi (default: target in deploy.local.json)
```

Everything private stays on the Pi, outside the repo: the MQTT login in
`~/.config/pi-health/config.json`, and the touch calibration, rotation and view in
`~/.config/wifi-meter/`.

The polkit rule lets the `pi` user trigger Wi-Fi rescans outside the desktop session
(otherwise NetworkManager only returns its cached list) and restart `wifi-meter.service`
without sudo.

## Run by hand on the Pi

```sh
cd ~/pi_display
sudo systemctl stop wifi-meter         # free the screen first
python3 -m wifi_meter --interval 5
python3 -m wifi_meter --demo                           # 16 fake networks, exercises paging
python3 -m wifi_meter --preview /tmp/meter.png          # live scan to PNG
python3 -m wifi_meter --preview /tmp/meter.png --demo --first 10   # sample data, second page
python3 -m wifi_meter --preview /tmp/server.png --view server --demo # server view, sample issues
```
