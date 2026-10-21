"""Raise or clear an alert on the LAN MQTT bus as this device (alerts/<hostname>/<id>).

    python3 -m wifi_meter.notify "Door open" "Garage door has been open 10 min" --level warning
    python3 -m wifi_meter.notify "Back soon" --expires 600
    python3 -m wifi_meter.notify --id door-open --clear
"""
import argparse
import sys

from . import bus


def main() -> None:
    parser = argparse.ArgumentParser(prog="wifi_meter.notify", description=__doc__.splitlines()[0])
    parser.add_argument("title", nargs="?", default="")
    parser.add_argument("message", nargs="?", default="")
    parser.add_argument("--level", choices=list(bus.LEVELS), default="info")
    parser.add_argument("--id", help="stable alert id (default: derived from the title)")
    parser.add_argument("--expires", type=int, metavar="SECONDS", help="auto-clear after this long")
    parser.add_argument("--clear", action="store_true", help="clear the alert with this id")
    args = parser.parse_args()

    if not (args.title or args.message or args.clear):
        parser.error("give a title and/or message, or --clear --id ID")
    if args.clear and not (args.id or args.title):
        parser.error("--clear needs --id (or the title it was raised with)")
    config = bus.load_config()
    if not config:
        sys.exit(f"no MQTT login at {bus.CREDENTIALS_FILE} (deploy the pi-health agent first)")
    alert_id = bus.alert_id(args.id or args.title or args.message)
    topic = bus.send_alert(config, alert_id, args.level, args.title, args.message,
                           args.expires, clear=args.clear)
    print(f"{'cleared' if args.clear else 'sent'} {topic}")


if __name__ == "__main__":
    main()
