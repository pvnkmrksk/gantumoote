# SPDX-License-Identifier: AGPL-3.0-or-later
"""Match registered Gantu advertisements on BlueZ or in a saved btmon capture."""

import argparse
import base64
import json
import logging
import signal
import sqlite3
import struct
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .hubble_ble import decode_hubble, hubble_identities, match_hubble
from .settings import DATA, write_private

SNOOP_HEADER = b"btsnoop\x00" + struct.pack(">II", 1, 2001)
SNOOP_EPOCH = 0x00DCDDB30F2F8000
KEYS = DATA / "secrets/tag-keys"
STATUS = DATA / "ble-live.json"
HISTORY = "ble-receptions.sqlite3"
HUBBLE_UUID = "0000fca6-0000-1000-8000-00805f9b34fb"


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def findmy_addresses(directory):
    identities = {}
    for path in directory.glob("*.keys"):
        tag = path.stem.replace("_", "-").capitalize()
        if not (tag.startswith("Hagalu-") or tag.startswith("Irulu-")):
            continue
        try:
            prefix = "Advertisement key: "
            line = next(line for line in path.read_text().splitlines() if line.startswith(prefix))
            key = base64.b64decode(line[len(prefix):], validate=True)
            if len(key) != 28:
                raise ValueError("Invalid key length")
        except (OSError, ValueError, StopIteration):
            logging.warning("Invalid Find My identity for %s", tag)
            continue
        address = bytes((key[0] | 0xC0,)) + key[1:6]
        identity = key[6:] + bytes((key[0] >> 6,))
        identities[address, identity] = tag
    return identities


def match_findmy(identities, address, data):
    if len(data) == 27 and data[:2] == b"\x12\x19":
        return identities.get((address, data[3:26]))


def findmy_and_hubble_data(data):
    offset = 0
    while offset < len(data) and data[offset]:
        end = offset + 1 + data[offset]
        if end > len(data):
            return
        if data[offset + 1:end].startswith(b"\xff\x4c\x00"):
            yield "findmy", data[offset + 4:end]
        if data[offset + 1:end].startswith(b"\x16\xa6\xfc"):
            yield "hubble", data[offset + 4:end]
        offset = end


def advertising_reports(packet):
    if len(packet) < 4 or packet[0] != 0x3E:
        return
    subevent, count = packet[2:4]
    if subevent == 2:
        offset, size, addr_start, length_at = 4, 9, 2, 8
    elif subevent == 0x0D:
        offset, size, addr_start, length_at = 4, 24, 3, 23
    else:
        return
    for _ in range(count):
        if len(packet) < offset + size:
            return
        address = packet[offset + addr_start:offset + addr_start + 6][::-1]
        end = offset + size + packet[offset + length_at]
        next_offset = end + (subevent == 2)
        if len(packet) < next_offset:
            return
        rssi = int.from_bytes(packet[end:end + 1] if subevent == 2 else packet[offset + 13:offset + 14],
                              "little", signed=True)
        for network, data in findmy_and_hubble_data(packet[offset + size:end]):
            yield network, address, data, rssi
        offset = next_offset


def heard(stats, tag, epoch, rssi, status_byte):
    previous = stats.get(tag, {})
    stats[tag] = {
        "first_seen": previous.get("first_seen", stamp(epoch)),
        "last_seen": stamp(epoch),
        "reports": previous.get("reports", 0) + 1,
        "rssi": rssi if rssi is not None and -127 <= rssi <= 20 else None,
        "status_byte": status_byte,
    }


def analyze_capture(path, keys=KEYS):
    findmy = findmy_addresses(keys)
    hubble = hubble_identities(keys)
    stats = {}
    started = ended = None
    with path.open("rb") as file:
        if file.read(16) != SNOOP_HEADER:
            raise ValueError("Expected a Linux btmon btsnoop capture")
        while header := file.read(24):
            if len(header) < 24:
                break
            _, length, flags, _, timestamp = struct.unpack(">IIIIq", header)
            if length > 65535:
                raise ValueError("Invalid btsnoop packet size")
            packet = file.read(length)
            if len(packet) != length:
                break
            ended = (timestamp - SNOOP_EPOCH) / 1e6
            if started is None:
                started = ended
            if flags & 0xFFFF != 3:
                continue
            for network, address, data, rssi in advertising_reports(packet):
                if network == "findmy":
                    if tag := match_findmy(findmy, address, data):
                        heard(stats, tag, ended, rssi, data[2])
                elif tag := match_hubble(hubble, data):
                    heard(stats, tag, ended, rssi, None)
    return {
        "file": path.name,
        "started_at": stamp(started) if started is not None else None,
        "ended_at": stamp(ended) if ended is not None else None,
        "tags": dict(sorted(stats.items())),
    }


def log_receptions(root, pending):
    if not pending:
        return
    with closing(sqlite3.connect(root / HISTORY)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS receptions (
            tag TEXT, minute TEXT, reports INTEGER, last_seen TEXT, rssi INTEGER, status_byte INTEGER,
            PRIMARY KEY (tag, minute)
        )""")
        db.executemany("""INSERT INTO receptions VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tag, minute) DO UPDATE SET reports=reports + excluded.reports,
            last_seen=excluded.last_seen, rssi=excluded.rssi, status_byte=excluded.status_byte
        """, ((tag, minute, item["reports"], item["last_seen"], item["rssi"], item["status_byte"])
              for (tag, minute), item in pending.items()))
    pending.clear()


def reception_history(root, since):
    path = root / HISTORY
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        return db.execute("""SELECT tag, minute, reports, last_seen, rssi, status_byte FROM receptions
            WHERE minute >= ? ORDER BY minute DESC, tag""", (since,)).fetchall()


def scan(adapter, on_packet, *, on_tick=None, refresh_seconds=10):
    """Watch matching advertisements from this BlueZ discovery session."""
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = f"/org/bluez/{adapter}"
    scanner = dbus.Interface(bus.get_object("org.bluez", adapter_path), "org.bluez.Adapter1")
    findmy = findmy_addresses(KEYS)
    hubble = hubble_identities(KEYS)
    stopping = False
    loop = GLib.MainLoop()

    def received(properties, path):
        apple = properties.get("ManufacturerData", {}).get(0x004C)
        service = properties.get("ServiceData", {}).get(HUBBLE_UUID)
        address_part = path.removeprefix(adapter_path + "/dev_")
        if address_part == path or (apple is None and service is None):
            return
        matches = []
        if apple is not None:
            try:
                address = bytes.fromhex(address_part.replace("_", ""))
            except ValueError:
                return
            apple = bytes(apple)
            if tag := match_findmy(findmy, address, apple):
                raw = bytes((len(apple) + 3, 0xFF, 0x4C, 0x00)) + apple
                matches.append((tag, "findmy", raw, apple))
        if service is not None:
            service = bytes(service)
            if decoded := decode_hubble(hubble, service):
                tag, payload = decoded
                raw = bytes((len(service) + 3, 0x16, 0xA6, 0xFC)) + service
                matches.append((tag, "hubble", raw, payload))
        if not matches:
            return
        rssi = properties.get("RSSI")
        if rssi is None:
            device = bus.get_object("org.bluez", path)
            try:
                rssi = device.Get("org.bluez.Device1", "RSSI", dbus_interface="org.freedesktop.DBus.Properties")
            except dbus.DBusException:
                rssi = 127
        now = time.time()
        rssi = int(rssi)
        for tag, network, raw, payload in matches:
            on_packet(tag, network, now, rssi if -127 <= rssi <= 20 else None, raw, payload)

    def changed(interface, properties, _invalidated, path):
        if interface == "org.bluez.Device1":
            received(properties, path)

    def added(path, interfaces):
        received(interfaces.get("org.bluez.Device1", {}), path)

    def discovery_changed(interface, properties, _invalidated, path):
        if path == adapter_path and interface == "org.bluez.Adapter1" and "Discovering" in properties and not properties["Discovering"]:
            loop.quit()

    bus.add_signal_receiver(changed, "PropertiesChanged", "org.freedesktop.DBus.Properties",
                            bus_name="org.bluez", path_keyword="path")
    bus.add_signal_receiver(discovery_changed, "PropertiesChanged", "org.freedesktop.DBus.Properties",
                            bus_name="org.bluez", path_keyword="path")
    bus.add_signal_receiver(added, "InterfacesAdded", "org.freedesktop.DBus.ObjectManager", bus_name="org.bluez")
    bus.watch_name_owner("org.bluez", lambda owner: None if owner else loop.quit())
    scanner.SetDiscoveryFilter({"Transport": "le", "DuplicateData": dbus.Boolean(True)})

    def update_keys():
        nonlocal findmy, hubble
        findmy = findmy_addresses(KEYS)
        hubble = hubble_identities(KEYS)
        return True

    def tick():
        try:
            on_tick()
            return True
        except (OSError, sqlite3.Error):
            logging.exception("Could not update BLE output")
            loop.quit()
            return False

    def stop():
        nonlocal stopping
        stopping = True
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    scanner.StartDiscovery()
    try:
        if on_tick:
            on_tick()
            GLib.timeout_add_seconds(refresh_seconds, tick)
        GLib.timeout_add_seconds(10, update_keys)
        loop.run()
        if not stopping:
            raise SystemExit("BlueZ discovery stopped")
    finally:
        try:
            scanner.StopDiscovery()
        except dbus.DBusException:
            pass


def monitor(adapter):
    stats = {}
    pending = {}
    started = stamp(time.time())

    def packet(tag, network, epoch, rssi, _raw, payload):
        minute = stamp(epoch)[:16]
        status_byte = payload[2] if network == "findmy" else None
        heard(stats, tag, epoch, rssi, status_byte)
        heard(pending, (tag, minute), epoch, rssi, status_byte)

    def publish():
        log_receptions(DATA, pending)
        write_private(STATUS, json.dumps({
            "started_at": started, "updated_at": stamp(time.time()), "tags": stats,
        }, sort_keys=True).encode())

    try:
        scan(adapter, packet, on_tick=publish)
    finally:
        log_receptions(DATA, pending)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_subparsers(dest="mode", required=True)
    live = mode.add_parser("monitor", help="run a persistent BlueZ LE scan")
    live.add_argument("--adapter", default="hci0")
    inspect = mode.add_parser("capture", help="summarize registered tags in an existing btmon file")
    inspect.add_argument("--keys", type=Path, default=KEYS)
    inspect.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    if args.mode == "monitor":
        monitor(args.adapter)
    else:
        print(json.dumps([analyze_capture(path, args.keys) for path in args.files], indent=2))


if __name__ == "__main__":
    main()
