# SPDX-License-Identifier: AGPL-3.0-or-later
"""List the Pi's known tags or stream their BLE advertisements."""

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import time
from collections import deque
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

from . import ble
from .ble_plot import PlotUpdates, latest_recording, render_recording
from .flash import NETWORK, TAG, read_json, records
from .phyphox import LightClient, LightFeed, timestamp
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


@dataclass(frozen=True)
class Sample:
    network: str
    epoch: float
    interval: float | None
    rssi: int | None
    temp: float | None
    raw: bytes


def interval_between(previous, current):
    if previous is not None and current >= previous:
        return current - previous
    return None


def value_text(value, unit, decimals):
    return "—" if value is None else f"{value:.{decimals}f} {unit}"


METRICS = (
    ("interval", "Interval", "s", 3, 10.0, "33"),
    ("rssi", "RSSI", "dBm", 0, 15.0, "36"),
    ("temp", "Temp", "°C", 1, 4.0, "35"),
)


def graph(points, start, now, width, lo, hi):
    """Render the most recent reading in each shared time bucket."""
    cells = ["·"] * width
    for epoch, value in points:
        if start <= epoch <= now and value is not None:
            column = min(width - 1, int((epoch - start) / (now - start) * width))
            level = int(max(0, min(7, round(7 * (value - lo) / (hi - lo)))))
            cells[column] = "▁▂▃▄▅▆▇█"[level]
    return "".join(cells)


def metric_scale(values, min_span, name):
    if name == "interval":
        return 0.0, max([min_span, *values])
    if not values:
        return 0.0, min_span
    lo, hi = min(values), max(values)
    padding = max(min_span - (hi - lo), 0.0) / 2
    return lo - padding, hi + padding


def draw(selected, history, received_counts, log_path, window, light_lines=None):
    now = time.time()
    start = now - window
    screen, height = shutil.get_terminal_size((120, 25))
    side_by_side = screen >= 34 * len(selected)
    width = (screen - 1 - 3 * (len(selected) - 1)) // len(selected) if side_by_side else max(3, screen - 1)
    recent = {tag: [sample for sample in history[tag] if sample.epoch >= start] for tag in selected}
    scale = {}
    for name, _, _, _, min_span, _ in METRICS:
        values = [getattr(sample, name) for samples in recent.values() for sample in samples]
        scale[name] = metric_scale([x for x in values if x is not None], min_span, name)

    columns = []
    for tag in selected:
        latest = history[tag][-1] if history[tag] else None
        lines = [(f"{short_name(tag)}  {tag}", "1")]
        if latest:
            observed = datetime.fromtimestamp(latest.epoch).strftime("%H:%M:%S.%f")[:-3]
            age = max(0, int(now - latest.epoch))
            lines.append((f"{observed}  {age}s ago  ·  {received_counts[tag]} heard", ""))
        else:
            lines.append(("Waiting for a BLE advert", ""))
        for name, label, unit, decimals, _, color in METRICS:
            value = getattr(latest, name) if latest else None
            title = f"{label:<8} {value_text(value, unit, decimals)}"
            points = [(sample.epoch, getattr(sample, name)) for sample in recent[tag]]
            intervals = [val for _, val in points if val is not None]
            if name == "interval" and intervals:
                title += f"  med {median(intervals):.2f}s"
            lines.append((title, color))
            lines.append((graph(points, start, now, width, *scale[name]), color))
        if latest:
            lines.append((f"{NETWORK[latest.network]} raw AD · {len(latest.raw)} B", "2"))
            lines.extend((row, "2") for row in textwrap.wrap(latest.raw.hex(" ").upper(), width=width,
                                                            break_long_words=False))
        columns.append(lines)

    def format_cell(cell, pad=None):
        value, style = cell
        value = value[:width].ljust(pad) if pad else value[:width]
        return f"\x1b[{style}m{value}\x1b[0m" if style and "NO_COLOR" not in os.environ else value

    if side_by_side:
        rows = [" │ ".join(format_cell(column[i] if i < len(column) else ("", ""), width)
                           for column in columns)
                for i in range(max(map(len, columns)))]
    else:
        rows = [format_cell(item) for column in columns for item in (*column, ("", ""))]
    heading = f"GANTU BLE   ·   last {window}s → now   ·   {datetime.fromtimestamp(now):%H:%M:%S}"
    footer = ["Ctrl-C to stop"]
    if log_path:
        footer.insert(0, f"Pi log: ~/blelog/{log_path.name}")
    light = light_lines(start, now, max(3, screen - 1)) if light_lines else []
    available = max(1, height - 2 - len(footer) - len(light))
    display = [heading[:screen - 1], "", *rows[:available], *light, *footer]
    sys.stdout.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in display) + "\x1b[J")
    sys.stdout.flush()


def draw_light(feed, start, now, width):
    status, samples = feed.snapshot()
    value = f"{samples[-1][2]:,.1f} lx" if samples else "—"
    observed = datetime.fromtimestamp(samples[-1][0]).strftime("%H:%M:%S.%f")[:-3] if samples else ""
    points = [(epoch, lux) for epoch, _, lux in samples if start <= epoch <= now]
    scale = metric_scale([lux for _, lux in points], 100.0, "illuminance")
    title = f"Phone light   {value}   {observed}   ·   {status}"
    return ["", title[:width], graph(points, start, now, width, *scale)]


def create_recording(directory, selected):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    names = "-".join(short_name(tag) for tag in selected)
    date = datetime.now().strftime("%Y%m%d_%H%M%S")
    fd, path = tempfile.mkstemp(prefix=f"{names}-{date}-", suffix=".tsv", dir=directory)
    return os.fdopen(fd, "w", encoding="utf-8", newline=""), Path(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-l", "--list", action="store_true", help="list known tags and their short names")
    mode.add_argument("-d", "--devices", nargs="+", metavar="TAG", help="stream tag names, e.g. -d h03 h04")
    mode.add_argument("--plot", metavar="TSV|latest", help="render a saved or latest recording as a private PNG")
    parser.add_argument("--plain", action="store_true", help="one line per advert, suitable for a file or pipe")
    parser.add_argument("--record", action="store_true", help="save a private TSV and PNG in ~/blelog on the Pi")
    parser.add_argument("--window", type=int, default=120, help="seconds shown in the live plots (default: 120)")
    parser.add_argument("--phyphox", metavar="URL", help="show and log the phone’s live Phyphox Light readings")
    parser.add_argument("--adapter", default="hci0")
    args = parser.parse_args()
    if args.plot:
        try:
            recording = latest_recording(Path.home() / "blelog") if args.plot == "latest" else Path(args.plot).expanduser()
            print(render_recording(recording))
        except ImportError:
            parser.exit(1, "BLE plot requires Matplotlib. Install python3-matplotlib on the Pi.\n")
        except (OSError, ValueError) as error:
            parser.exit(1, f"Could not plot recording: {error}\n")
        return
    if not 15 <= args.window <= 3600:
        parser.error("--window must be between 15 and 3600 seconds")
    try:
        client = LightClient(args.phyphox) if args.phyphox else None
    except ValueError as error:
        parser.error(str(error))
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
    history = {tag: deque(maxlen=4096) for tag in selected}
    received_counts = dict.fromkeys(selected, 0)
    previous = {}
    plain = args.plain or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb"
    log_file = log_path = writer = None
    if args.record:
        log_file, log_path = create_recording(Path.home() / "blelog", selected)
        writer = csv.writer(log_file, delimiter="\t")
        writer.writerow(("timestamp", "tag", "network", "rssi_dbm", "temp_c", "raw_ad_hex", "interval_s"))
        log_file.flush()
        if plain:
            print(f"Recording to {log_path} on the Pi", file=sys.stderr)
            print(f"Plot: {log_path.with_suffix('.png')}", file=sys.stderr)

    plots = PlotUpdates(log_path, [short_name(tag) for tag in selected]) if log_path else nullcontext()
    light_path = log_path.with_suffix(".lux.tsv") if client and log_path else None
    light = LightFeed(client, light_path, plain=plain) if client else None

    def packet(tag, network, epoch, rssi, raw, payload):
        if tag not in selected:
            return
        temp = temperature(tags[tag], network, payload)
        interval = interval_between(previous.get((tag, network)), epoch)
        previous[tag, network] = epoch
        if plain or writer is not None:
            observed = timestamp(epoch)
        if writer is not None:
            writer.writerow((observed, short_name(tag), network, rssi, temp, raw.hex().upper(),
                             f"{interval:.3f}" if interval is not None else ""))
            log_file.flush()
        if plain:
            print(f"{observed}  {short_name(tag)}  {NETWORK[network]}  "
                  f"interval {value_text(interval, 's', 3)}  RSSI {value_text(rssi, 'dBm', 0)}  "
                  f"temp {value_text(temp, '°C', 1)}  raw={raw.hex().upper()}", flush=True)
        else:
            history[tag].append(Sample(network, epoch, interval, rssi, temp, raw))
            received_counts[tag] += 1

    def refresh():
        render_light = (lambda start, now, width: draw_light(light, start, now, width)) if light else None
        draw(selected, history, received_counts, log_path, args.window, render_light)

    if plain:
        print(f"Watching {', '.join(selected)}", file=sys.stderr)
    if not plain:
        sys.stdout.write("\x1b[?25l")
    try:
        with plots, light if light else nullcontext():
            ble.scan(args.adapter, packet, on_tick=None if plain else refresh, refresh_seconds=1)
    finally:
        if log_file is not None:
            log_file.close()
        if not plain:
            print("\x1b[?25h" + (f"\nSaved on the Pi at {log_path}" if log_path else ""))
            if log_path and log_path.with_suffix(".png").is_file():
                print(f"Plot: {log_path.with_suffix('.png')}")
            if light_path:
                print(f"Phone log: {light_path}")


if __name__ == "__main__":
    main()
