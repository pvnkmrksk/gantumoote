# SPDX-License-Identifier: AGPL-3.0-or-later
"""List the Pi's known tags or stream their BLE advertisements."""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import textwrap
import time
from contextlib import closing
from datetime import datetime

from . import ble
from .flash import NETWORK, TAG, read_json, records
from .settings import DATA, REGISTRY

SHORT_TAG = re.compile(r"([hi])0*([0-9]+)", re.IGNORECASE)


def short_name(tag):
    family, number = tag.split("-")
    return f"{family[0].lower()}{int(number):02d}"


def resolve(value):
    value = value.strip()
    if match := SHORT_TAG.fullmatch(value):
        family = "Hagalu" if match[1].lower() == "h" else "Irulu"
        return f"{family}-{int(match[2]):03d}"
    return value.replace("_", "-").capitalize()


def inventory(root):
    tags = {}
    registry = root / REGISTRY
    if registry.is_file():
        with closing(sqlite3.connect(f"{registry.as_uri()}?mode=ro", uri=True)) as db:
            for name, provider, config in db.execute("SELECT name, provider, config FROM devices WHERE deleted=0"):
                tag = resolve(name)
                if TAG.fullmatch(tag):
                    entry = tags.setdefault(tag, {})
                    settings = json.loads(config)
                    if provider == "openhaystack":
                        entry.update(findmy_temp=settings.get("status_format") == "temp_half_c")
                    elif provider == "hubble":
                        entry.update(hubble_unit=settings.get("temp_unit") or 0.01)
    for tag, plan in read_json(root / "flash-plan.json", {}).items():
        tags.setdefault(tag, {}).update(plan)
    for tag, entry in records(root).items():
        tags.setdefault(tag, {}).update(entry)
    keys = root / "secrets/tag-keys"
    for tag in set(ble.findmy_addresses(keys).values()):
        tags.setdefault(tag, {}).setdefault("network", "findmy")
    for identities in ble.hubble_identities(keys).values():
        for tag, _key, _counter in identities:
            tags.setdefault(tag, {}).setdefault("network", "hubble")
    return dict(sorted(tags.items()))


def temperature(entry, network, payload):
    if network == "findmy" and (entry.get("findmy_temp") or entry.get("telemetry") == "findmy-temperature"):
        return int.from_bytes(payload[2:3], signed=True) / 2
    if network == "hubble" and len(payload) == 7:
        return int.from_bytes(payload[1:3], "little", signed=True) * entry.get("hubble_unit", 0.01)
    return None


def details(rssi, temp):
    return " · ".join((f"RSSI {rssi} dBm" if rssi is not None else "RSSI —",
                       f"Temp {temp:g} °C" if temp is not None else "Temp —"))


def draw(selected, latest):
    screen, _ = shutil.get_terminal_size((120, 25))
    side_by_side = screen >= 34 * len(selected)
    width = (screen - 1 - 3 * (len(selected) - 1)) // len(selected) if side_by_side else max(3, screen - 1)
    columns = []
    for tag in selected:
        title = f"{short_name(tag)}  {tag}"
        if tag not in latest:
            lines = [title, "Waiting for a BLE advert"]
        else:
            network, epoch, rssi, raw, temp = latest[tag]
            observed = datetime.fromtimestamp(epoch).strftime("%H:%M:%S.%f")[:-3]
            age = max(0, int(time.time() - epoch))
            lines = [title, f"{observed}  ({age}s ago)", details(rssi, temp),
                     f"{NETWORK[network]} raw AD · {len(raw)} B"]
            lines += textwrap.wrap(raw.hex(" ").upper(), width=width, break_long_words=False)
        columns.append(lines)
    if side_by_side:
        rows = [" │ ".join((column[index] if index < len(column) else "")[:width].ljust(width)
                           for column in columns).rstrip()
                for index in range(max(map(len, columns)))]
    else:
        rows = [line for column in columns for line in (*column, "")]
    sys.stdout.write("\x1b[H\x1b[J" + "\n".join(rows) + "\n\nCtrl-C to stop\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-l", "--list", action="store_true", help="list known tags and their short names")
    mode.add_argument("-d", "--devices", nargs="+", metavar="TAG", help="stream tag names, e.g. -d h03 h04")
    parser.add_argument("--plain", action="store_true", help="one line per advert, suitable for a file or pipe")
    parser.add_argument("--adapter", default="hci0")
    args = parser.parse_args()
    tags = inventory(DATA)
    if args.list:
        for tag, entry in tags.items():
            state = "OTP" if entry.get("burned_at") else "prepared"
            print(f"{short_name(tag):<6} {tag:<15} {entry.get('board', '—'):<7} "
                  f"{NETWORK.get(entry.get('network'), '—'):<17} {state}")
        return
    selected = list(dict.fromkeys(resolve(name) for name in args.devices))
    unknown = [name for name in selected if name not in tags]
    if unknown:
        parser.error(f"unknown tags: {', '.join(unknown)}. Use -l to list your tags.")
    latest = {}
    plain = args.plain or not sys.stdout.isatty()

    def packet(tag, network, epoch, rssi, raw, payload):
        if tag not in selected:
            return
        temp = temperature(tags[tag], network, payload)
        if plain:
            observed = datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")
            print(f"{observed}  {short_name(tag)}  {NETWORK[network]}  {details(rssi, temp)}  "
                  f"raw={raw.hex().upper()}", flush=True)
        else:
            latest[tag] = (network, epoch, rssi, raw, temp)
            draw(selected, latest)

    def refresh():
        draw(selected, latest)

    if plain:
        print(f"Watching {', '.join(selected)}", file=sys.stderr)
    try:
        ble.scan(args.adapter, packet, on_tick=None if plain else refresh, refresh_seconds=1)
    finally:
        if not plain:
            print()


if __name__ == "__main__":
    main()
