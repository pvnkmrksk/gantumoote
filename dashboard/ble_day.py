# SPDX-License-Identifier: AGPL-3.0-or-later
"""Summarize a local day of BLE receptions and Find My observations."""

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path

from .ble import reception_history
from .ble_plot import COLORS, RECORDING_NAME, draw_smooth, draw_trace, read_packets
from .ble_stream import inventory, resolve, short_name
from .settings import DATA, REGISTRY, write_private


def findmy_sources(root):
    registry = root / REGISTRY
    if not registry.is_file():
        return
    with closing(sqlite3.connect(f"{registry.as_uri()}?mode=ro", uri=True)) as db:
        devices = db.execute("SELECT name, config FROM devices WHERE provider='openhaystack' AND deleted=0").fetchall()
    for name, config in devices:
        database = json.loads(config).get("database")
        if database and (path := root / database).is_file():
            yield resolve(name), path


def report_times(root, start, end):
    """Read observation timestamps from every saved Find My entry, including archives."""
    reports = defaultdict(set)
    bounds = tuple(when.astimezone(timezone.utc).isoformat() for when in (start, end))
    for tag, path in findmy_sources(root):
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            times = db.execute("SELECT DISTINCT timestamp FROM sightings WHERE timestamp >= ? AND timestamp < ?", bounds).fetchall()
        if times:
            reports[tag].update(datetime.fromisoformat(row[0]).astimezone(start.tzinfo) for row in times)
    return reports


def day_bounds(day):
    return (datetime.combine(day, time.min).astimezone(),
            datetime.combine(day + timedelta(days=1), time.min).astimezone())


def day_data(day, root):
    start, end = day_bounds(day)
    after = start.astimezone(timezone.utc).isoformat()[:16]
    receptions = defaultdict(list)
    before = end.astimezone(timezone.utc).isoformat()[:16]
    for tag, minute, count, last, rssi, status in reversed(reception_history(root, after, before)):
        last_seen = datetime.fromisoformat(last).astimezone(start.tzinfo)
        if start <= last_seen < end:
            receptions[tag].append((datetime.fromisoformat(minute + "+00:00").astimezone(start.tzinfo),
                                    count, last_seen, rssi, status))
    return start, end, receptions, report_times(root, start, end)


def exact_intervals(folder, start, end, tags):
    intervals = defaultdict(list)
    by_short = {short_name(tag): tag for tag in tags}
    for path in folder.glob("*.tsv"):
        match = RECORDING_NAME.fullmatch(path.stem)
        if not match or match[2][:8] > end.strftime("%Y%m%d") or path.stat().st_mtime < start.timestamp():
            continue
        for short, packets in read_packets(path).items():
            if short not in by_short:
                continue
            intervals[by_short[short]].extend((when, gap) for when, gap, _, _ in packets
                                               if gap is not None and gap > 0 and start <= when < end)
    return intervals


def day_plot_path(folder, day, selected=(), *, diagnostics=False):
    names = "-".join(short_name(tag) for tag in selected) or "all"
    suffix = "diagnostics" if diagnostics else "day"
    return folder / f"{names}-{day.isoformat()}-{suffix}.png"


def render_day(day, selected=(), root=DATA, folder=None):
    from matplotlib import dates, ticker
    from matplotlib.figure import Figure

    folder = folder if folder is not None else Path.home() / "blelog"
    start, day_end, local, reports = day_data(day, root)
    now = datetime.now().astimezone(start.tzinfo)
    if now < start:
        raise ValueError(f"{day} is in the future")
    end = min(day_end, now)
    tags = list(selected or sorted(set(local) | set(reports)))
    exact = exact_intervals(folder, start, end, tags)
    metadata = inventory(root)
    fig = Figure(figsize=(13, 9.5 + .16 * max(0, len(tags) - 4)))
    hits, gaps, strength, temp, findmy = axes = fig.subplots(
        5, 1, sharex=True, gridspec_kw={"height_ratios": [1.4, 1.15, 1.3, 1.2, 1 + .21 * max(0, len(tags) - 4)]}
    )
    fig.subplots_adjust(left=.125, right=.965, top=.827, bottom=.105, hspace=.56)
    ink, muted, grid = "#192638", "#46566D", "#E1E6EC"
    fig.text(.125, .958, "BLE and Find My across the day", color=ink, size=24, weight="bold", va="top")
    fig.text(.125, .908, f"{start:%d %b %Y}  ·  through {end:%H:%M} {end:%Z}  ·  Pi reception history",
             color=muted, size=11)
    fig.text(.125, .868, "     ".join(f"{short_name(tag)} · {sum(row[1] for row in local[tag]):,} BLE / {len(reports[tag])} Find My"
                                   for tag in tags[:4]), color=muted, size=9)
    for ax, title, unit in zip(
            axes,
            ("Local BLE receptions", "Packet-by-packet reception gaps", "Signal strength",
             "Decoded temperature", "Find My observations"),
            ("per 5 min", "Seconds", "dBm", "°C", "")):
        ax.set_title(title, loc="left", fontsize=11, color=ink, weight="bold", pad=6)
        ax.set_ylabel(unit, color=muted)
        ax.set_axisbelow(True)
        ax.grid(axis="x" if ax is findmy else "both", color=grid, linewidth=.7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["bottom", "left"]].set_color(grid)
        ax.tick_params(length=0, pad=6, labelcolor=muted)
    for ax, caption in (
        (gaps, "Detailed TSVs only"),
        (strength, "Last reception per minute · centered weighted average"),
        (temp, "Last decoded reading per minute · centered weighted average"),
    ):
        ax.set_title(caption, loc="right", fontsize=8.5, color=muted, pad=6)
    if not any(local.values()) and not any(reports.values()):
        hits.text(.015, .5, "No BLE or Find My observations for this day", transform=hits.transAxes,
                  va="center", color=muted)
    if any(exact.values()):
        gaps.set_yscale("log", base=2)
        gaps.yaxis.set_major_locator(ticker.LogLocator(base=2, numticks=8))
        gaps.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value:g}"))
        gaps.yaxis.set_minor_locator(ticker.NullLocator())
        gaps.set_ylabel("Seconds (log scale)", color=muted)
    else:
        gaps.set_yticks([])
        gaps.text(.015, .5, "No detailed packet gaps available", transform=gaps.transAxes, va="center", color=muted)

    bins = [start + timedelta(minutes=5 * n)
            for n in range(int((end - start).total_seconds() // 300) + 2)]
    findmy.set_yticks(range(len(tags)), [short_name(tag) for tag in tags])
    findmy.set_ylim(max(1, len(tags)) - .45, -.55)
    findmy.spines["left"].set_visible(False)
    for lane, tag in enumerate(tags):
        color = COLORS[lane % len(COLORS)]
        counts = defaultdict(int)
        for minute, count, *_ in local[tag]:
            counts[int((minute - start).total_seconds() // 300)] += count
        if counts:
            hits.step(bins, [counts[i] for i in range(len(bins))], where="post", color=color, lw=1.65)
        draw_trace(gaps, sorted(exact[tag]), color)
        rssi = [(last, value) for _, _, last, value, _ in local[tag] if value is not None]
        entry = metadata.get(tag, {})
        findmy_temp = entry.get("findmy_temp") or entry.get("telemetry") == "findmy-temperature"
        temps = [(last, int.from_bytes(bytes([status]), signed=True) / 2)
                 for _, _, last, _, status in local[tag] if findmy_temp and status is not None]
        for ax, points in ((strength, rssi), (temp, temps)):
            draw_trace(ax, points, color)
            draw_smooth(ax, points, color)
        times = sorted(reports[tag])
        findmy.axhline(lane, color=grid, lw=.8)
        findmy.get_yticklabels()[lane].set(color=color if counts or times else muted, weight="bold")
        if times:
            findmy.scatter(times, [lane] * len(times), marker="|", s=58, color=color, linewidths=1.1, zorder=3)
        else:
            findmy.text(.013, lane, "No Find My observations", va="center", color=muted, size=9,
                        transform=findmy.get_yaxis_transform(),
                        bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5})
    if end < day_end:
        for ax in axes:
            ax.axvspan(end, day_end, color="#F1F4F7", zorder=0)
    findmy.set_xlim(start, day_end)
    findmy.xaxis.set_major_locator(dates.AutoDateLocator(tz=start.tzinfo, minticks=5, maxticks=13))
    findmy.xaxis.set_major_formatter(dates.DateFormatter("%H:%M", tz=start.tzinfo))
    fig.text(.125, .034, "Find My ticks use observation time. Local BLE counts include receptions only.",
             color=muted, size=9)

    path = day_plot_path(folder, day, selected)
    with BytesIO() as image:
        fig.savefig(image, format="png", dpi=160, backend="agg")
        write_private(path, image.getvalue())
    return path
