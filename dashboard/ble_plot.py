# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a BLE recording and its optional Phyphox sibling as a private PNG."""

import csv
import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from statistics import median
from threading import Event, Thread

from .settings import write_private

RECORDING_NAME = re.compile(r"((?:[hi][0-9]+-)+)([0-9]{8}_[0-9]{6})(?:-[^.]+)?")
REFRESH_SECONDS = 30
SMOOTH_RADIUS = 4
COLORS = ("#1168AB", "#AF4B08", "#8B409E", "#1D7859", "#A63559", "#57662A")


def rows(path, *columns):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if not set(columns).issubset(reader.fieldnames or ()):
            raise ValueError(f"{path.name} requires columns: {', '.join(columns)}")
        for row in reader:
            if all(row.get(column) is not None for column in columns):
                yield row


def measured(value):
    if not value:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def read_packets(path):
    tags = defaultdict(list)
    for row in rows(path, "timestamp", "tag", "network", "interval_s", "rssi_dbm", "temp_c"):
        when = datetime.fromisoformat(row["timestamp"])
        if when.tzinfo is None:
            raise ValueError(f"{path.name} timestamps need a UTC offset")
        tags[row["tag"]].append((when, measured(row["interval_s"]),
                                 measured(row["rssi_dbm"]), measured(row["temp_c"])))
    for packets in tags.values():
        packets.sort(key=lambda packet: packet[0])
    return tags


def read_light(path):
    if not path.is_file() or path.stat().st_size == 0:
        return []
    samples = []
    for row in rows(path, "timestamp", "lux", "experiment_s"):
        lux = measured(row["lux"])
        if lux is not None:
            when = datetime.fromisoformat(row["timestamp"])
            if when.tzinfo is None:
                raise ValueError(f"{path.name} timestamps need a UTC offset")
            samples.append((when, lux))
    return sorted(samples)


def draw_trace(ax, points, color):
    if points:
        times, values = zip(*points)
        ax.scatter(times, values, s=14, color=color, alpha=.62, linewidths=0, zorder=3)


def draw_smooth(ax, points, color):
    """Draw a centered triangular moving average with gaps at long silences."""
    if len(points) < 3:
        return
    times, values = zip(*points)
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    positive = [gap for gap in gaps if gap > 0]
    if not positive:
        return
    max_gap = 4 * median(positive)
    boundaries = [0, *(i for i, gap in enumerate(gaps, 1) if gap > max_gap), len(points)]
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start < 3:
            continue
        smoothed = []
        for i in range(start, end):
            neighbors = range(max(start, i - SMOOTH_RADIUS), min(end, i + SMOOTH_RADIUS + 1))
            weights = [SMOOTH_RADIUS + 1 - abs(i - j) for j in neighbors]
            smoothed.append(sum(values[j] * weight for j, weight in zip(neighbors, weights)) / sum(weights))
        ax.plot(times[start:end], smoothed, color=color, linewidth=1.8, zorder=4)


def latest_recording(directory):
    recordings = ((match[2], path) for path in directory.glob("*.tsv")
                  if (match := RECORDING_NAME.fullmatch(path.stem)))
    latest = max(recordings, default=None)
    if latest is None:
        raise FileNotFoundError(f"No BLE recordings in {directory}")
    return latest[1]


def render_recording(path, selected=()):
    from matplotlib import dates, ticker
    from matplotlib.figure import Figure

    path = Path(path)
    packets = read_packets(path)
    light = read_light(path.with_suffix(".lux.tsv"))
    if not selected and (match := RECORDING_NAME.fullmatch(path.stem)):
        selected = match[1].rstrip("-").split("-")
    tags = list(dict.fromkeys((*selected, *packets)))
    first = min((samples[0][0] for samples in packets.values()), default=None)
    last = max((samples[-1][0] for samples in packets.values()), default=None)
    start = min(first, light[0][0]) if first and light else first or (light[0][0] if light else None)
    end = max(last, light[-1][0]) if last and light else last or (light[-1][0] if light else None)
    modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    start, end = start or modified, end or modified
    zone = start.tzinfo

    fig = Figure(figsize=(12, 8.25 + .16 * max(0, len(tags) - 4) + (1.3 if light else 0)))
    ratios = [1 + .21 * max(0, len(tags) - 4), 1.65, 1.38, 1.22]
    if light:
        ratios.append(1.22)
    axes = fig.subplots(len(ratios), 1, sharex=True, gridspec_kw={"height_ratios": ratios})
    fig.subplots_adjust(left=.125, right=.965, top=.827, bottom=.105, hspace=.51)
    arrivals, intervals, signal, temperature = axes[:4]
    style = dict(color="#192638", fontsize=11, fontweight="bold")
    muted, grid = "#46566D", "#E1E6EC"
    fig.text(.125, .957, "BLE recording", color=style["color"], size=24, weight="bold", va="top")
    end_local = end.astimezone(zone)
    span_end = end_local.strftime("%H:%M:%S" if start.date() == end_local.date() else "%d %b %H:%M:%S")
    fig.text(.125, .902, f"{start:%d %b %Y  ·  %H:%M:%S}–{span_end}  ·  {zone.tzname(start)}",
             color=muted, size=10)

    titles = ["Adverts received", "Time between received adverts", "Signal strength", "Decoded temperature"]
    units = ["", "Seconds", "dBm", "°C"]
    if light:
        titles.append("Phone light")
        units.append("lx")
    for ax, title, unit in zip(axes, titles, units):
        ax.set_title(title, loc="left", pad=7, **style)
        ax.set_ylabel(unit, color=muted)
        ax.set_axisbelow(True)
        ax.grid(axis="x" if ax is arrivals else "both", color=grid, linewidth=.7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["bottom", "left"]].set_color(grid)
        ax.tick_params(length=0, pad=6, labelcolor=muted)

    arrivals.spines[["left", "bottom"]].set_visible(False)
    colors = [COLORS[index % len(COLORS)] for index in range(len(tags))]
    arrivals.set_yticks(range(len(tags)), [f"{tag}  ({len(packets[tag])})" for tag in tags])
    arrivals.set_ylim(max(1, len(tags)) - .45, -.55)
    has_gaps = has_rssi = has_temp = False
    for lane, (tag, color) in enumerate(zip(tags, colors)):
        readings = packets[tag]
        arrivals.get_yticklabels()[lane].set(color=color if readings else muted, weight="bold")
        arrivals.axhline(lane, color=grid, lw=.8)
        if not readings:
            arrivals.text(.013, lane, "No adverts received", va="center", color=muted, size=9,
                          transform=arrivals.get_yaxis_transform(),
                          bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5})
            continue
        arrivals.scatter([row[0] for row in readings], [lane] * len(readings),
                         marker="|", s=58, color=color, linewidths=1.1, zorder=3)
        gaps = [(when, gap) for when, gap, _, _ in readings if gap is not None and gap > 0]
        rssi = [(when, value) for when, _, value, _ in readings if value is not None]
        temps = [(when, value) for when, _, _, value in readings if value is not None]
        has_gaps |= bool(gaps)
        has_rssi |= bool(rssi)
        has_temp |= bool(temps)
        draw_trace(intervals, gaps, color)
        if len(gaps) >= 5:
            times, values = zip(*gaps)
            intervals.plot(times[4:], [median(values[i - 4:i + 1]) for i in range(4, len(values))],
                           color=color, linewidth=1.8, zorder=4)
        draw_trace(signal, rssi, color)
        draw_smooth(signal, rssi, color)
        draw_trace(temperature, temps, color)
        draw_smooth(temperature, temps, color)

    if has_gaps:
        intervals.set_yscale("log", base=2)
        intervals.yaxis.set_major_locator(ticker.LogLocator(base=2, numticks=8))
        intervals.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value:g}"))
        intervals.yaxis.set_minor_locator(ticker.NullLocator())
        intervals.set_ylabel("Seconds (log scale)", color=muted)
    smooth_note = f"Centered weighted average of up to {2 * SMOOTH_RADIUS + 1} readings"
    for ax, present, note, empty in (
        (intervals, has_gaps, "Lines show the trailing median of 5 gaps", "Waiting for a second advert"),
        (signal, has_rssi, smooth_note, "No RSSI readings"),
        (temperature, has_temp, smooth_note, "No decoded temperatures"),
    ):
        if present and note:
            ax.set_title(note, loc="right", fontsize=8.5, color=muted)
        if not present:
            ax.set_yticks([])
            ax.text(.013, .5, empty, transform=ax.transAxes, va="center", color=muted, size=9)
    if not tags:
        arrivals.text(.013, .5, "No adverts received", transform=arrivals.transAxes, va="center", color=muted, size=9)
    if light:
        draw_trace(axes[-1], light, "#756018")

    span = end - start
    padding = max(timedelta(seconds=5), min(span * .025, timedelta(minutes=2)))
    axes[-1].set_xlim(start - padding, end + padding)
    if start.date() != end_local.date():
        time_format = "%d %b\n%H:%M"
    else:
        time_format = "%H:%M:%S" if span < timedelta(minutes=5) else "%H:%M"
    axes[-1].xaxis.set_major_locator(dates.AutoDateLocator(tz=zone, minticks=4, maxticks=9))
    axes[-1].xaxis.set_major_formatter(dates.DateFormatter(time_format, tz=zone))
    fig.text(.125, .035, "Reception gaps can include missed adverts.",
             color=muted, size=9)

    output = path.with_suffix(".png")
    with BytesIO() as image:
        fig.savefig(image, format="png", dpi=160, backend="agg")
        write_private(output, image.getvalue())
    return output


class PlotUpdates:
    """Refresh a single recording away from the BLE event loop."""

    def __init__(self, path, selected):
        self.path = path
        self.selected = selected
        self.light = path.with_suffix(".lux.tsv")
        self.done = Event()
        self.worker = Thread(name="ble-plot", target=self.run, daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.worker.join()

    def run(self):
        previous = None
        while True:
            stopping = self.done.is_set()
            try:
                signature = tuple(path.stat().st_size if path.exists() else None
                                  for path in (self.path, self.light))
                if signature != previous:
                    render_recording(self.path, self.selected)
                    previous = signature
            except ImportError:
                logging.warning("BLE plot requires Matplotlib. Install python3-matplotlib on the Pi.")
                return
            except (OSError, ValueError):
                logging.exception("Could not plot %s", self.path)
                return
            if stopping:
                return
            self.done.wait(REFRESH_SECONDS)
