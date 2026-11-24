# SPDX-License-Identifier: AGPL-3.0-or-later
"""Summarize a local day of BLE receptions and Find My observations."""

import csv
import json
from math import ceil
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path

from .ble import reception_history
from .ble_plot import COLORS, RECORDING_NAME, draw_smooth, draw_trace, read_packets, smooth_segments
from .ble_stream import inventory, resolve, short_name
from .flash import TAG
from .settings import DATA, REGISTRY, write_private


MATCH_WINDOW = timedelta(seconds=10)
DAY_AXES = {"left": .125, "right": .965, "top": .827, "bottom": .105}


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


def report_observations(root, start, end):
    """Read Find My observation times and any status copied from the advert."""
    reports = defaultdict(lambda: defaultdict(set))
    bounds = tuple(when.astimezone(timezone.utc).isoformat() for when in (start, end))
    for tag, path in findmy_sources(root):
        if not TAG.fullmatch(tag):
            continue
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(sightings)")}
            status_column = "status" if "status" in cols else "NULL"
            for when, status in db.execute(
                    f"SELECT DISTINCT timestamp,{status_column} FROM sightings WHERE timestamp >= ? AND timestamp < ?",
                    bounds):
                statuses = reports[tag][datetime.fromisoformat(when).astimezone(start.tzinfo)]
                if status is not None:
                    statuses.add(status)
    return reports


def report_times(root, start, end):
    return defaultdict(set, {tag: set(times) for tag, times in report_observations(root, start, end).items()})


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
        if TAG.fullmatch(tag) and start <= last_seen < end:
            receptions[tag].append((datetime.fromisoformat(minute + "+00:00").astimezone(start.tzinfo),
                                    count, last_seen, rssi, status))
    return start, end, receptions, report_observations(root, start, end)


def detailed_packets(folder, start, end, tags):
    packets = defaultdict(list)
    continuous = defaultdict(list)
    by_short = {short_name(tag): tag for tag in tags}
    for path in folder.glob("*.tsv"):
        match = RECORDING_NAME.fullmatch(path.stem)
        if not match or match[2][:8] > end.strftime("%Y%m%d") or path.stat().st_mtime < start.timestamp():
            continue
        for short, readings in read_packets(path).items():
            if short not in by_short:
                continue
            tag = by_short[short]
            inside = [packet for packet in readings if start <= packet.when < end]
            packets[tag].extend(inside)
            if path.stem.startswith("known-"):
                continuous[tag].extend(inside)
    return packets, continuous


def findmy_matches(observations, packets, continuous=()):
    """Find timing candidates without assigning a unique packet to an ambiguous report."""
    def by_time(items):
        deduplicated = {packet.when: packet for packet in items if packet.network == "findmy"}
        times = sorted(deduplicated)
        return times, [deduplicated[when] for when in times]

    def nearby(index, when):
        times, readings = index
        return readings[bisect_left(times, when - MATCH_WINDOW):bisect_right(times, when + MATCH_WINDOW)]

    preferred = by_time(continuous)
    all_packets = by_time(packets)
    for when, statuses in sorted(observations.items()):
        candidates = nearby(preferred, when) or nearby(all_packets, when)
        yield when, tuple(packet for packet in candidates
                          if not statuses or packet.status is None or packet.status in statuses)


def day_matches(day, selected, root, folder):
    start, end = day_bounds(day)
    observations = report_observations(root, start, end)
    tags = selected or sorted(observations)
    packets, continuous = detailed_packets(folder, start - MATCH_WINDOW, end + MATCH_WINDOW, tags)
    for tag in tags:
        for when, candidates in findmy_matches(observations[tag], packets[tag], continuous[tag]):
            yield tag, when, observations[tag][when], candidates


def matches_csv(day, selected=(), root=DATA, folder=None):
    folder = folder if folder is not None else Path.home() / "blelog"
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(("tag", "find_my_observed_at", "candidates", "pi_received_at", "pi_minus_find_my_s",
                     "rssi_dbm", "temp_c", "status_agrees"))
    for tag, when, statuses, candidates in day_matches(day, selected, root, folder):
        if not candidates:
            writer.writerow((tag, when.isoformat(), 0, "", "", "", "", ""))
        for packet in candidates:
            status_agrees = "yes" if statuses and packet.status is not None else ""
            writer.writerow((tag, when.isoformat(), len(candidates), packet.when.isoformat(),
                             f"{(packet.when - when).total_seconds():.3f}", packet.rssi, packet.temp, status_agrees))
    return stream.getvalue()


def time_rows(points):
    return [(round(when.timestamp() * 1000), round(value, 3)) for when, value in points]


def extrema_sample(points, limit=1000):
    """Keep local maxima and minima in dense packet traces."""
    if len(points) <= limit:
        return points
    stride = ceil(len(points) / (limit // 2))
    sampled = [points[0]]
    for i in range(0, len(points), stride):
        group = points[i:i + stride]
        sampled.extend(sorted({min(group, key=lambda p: p[1]), max(group, key=lambda p: p[1])}))
    sampled.append(points[-1])
    return sampled


def minute_traces(rows, entry):
    rssi = [(last, value) for _, _, last, value, _ in rows if value is not None]
    findmy_temp = entry.get("findmy_temp") or entry.get("telemetry") == "findmy-temperature"
    temp = [(last, int.from_bytes(bytes([status]), signed=True) / 2)
            for _, _, last, _, status in rows if findmy_temp and status is not None]
    return rssi, temp


def minute_counts(rows, start):
    counts = defaultdict(int)
    for minute, count, *_ in rows:
        counts[int((minute - start).total_seconds() // 300)] += count
    return counts


def packet_gaps(packets, start, end):
    return sorted((p.when, p.gap) for p in packets if p.gap and p.gap > 0 and start <= p.when < end)


def match_events(day, selected, root=DATA, folder=None):
    folder = folder if folder is not None else Path.home() / "blelog"
    start, end, local, observations = day_data(day, root)
    through = min(end, datetime.now().astimezone(start.tzinfo))
    tags = selected or sorted(set(local) | set(observations))
    packets, continuous = detailed_packets(folder, start - MATCH_WINDOW, through + MATCH_WINDOW, tags)
    metadata = inventory(root)
    events = []
    series = []
    for index, tag in enumerate(tags):
        name = short_name(tag)
        rssi, temp = minute_traces(local[tag], metadata.get(tag, {}))
        counts = minute_counts(local[tag], start)
        series.append({
            "tag": name, "color": COLORS[index % len(COLORS)],
            "hits": time_rows((start + timedelta(minutes=5 * step), count) for step, count in sorted(counts.items())),
            "gaps": time_rows(extrema_sample(packet_gaps(packets[tag], start, through))),
            "rssi": time_rows(rssi),
            "temp": time_rows(temp),
            "rssiTrend": [time_rows(segment) for segment in smooth_segments(rssi)],
            "tempTrend": [time_rows(segment) for segment in smooth_segments(temp)],
        })
        for when, candidates in findmy_matches(observations[tag], packets[tag], continuous[tag]):
            events.append({
                "tag": name,
                "observed": when.isoformat(timespec="seconds"),
                "candidates": [
                    {"received": p.when.astimezone(start.tzinfo).isoformat(timespec="milliseconds"),
                     "offset": round((p.when - when).total_seconds(), 3),
                     "gap": p.gap, "rssi": p.rssi, "temp": p.temp}
                    for p in candidates
                ],
            })
    events.sort(key=lambda event: event["observed"])
    return {"start": start.isoformat(), "end": end.isoformat(), "through": through.isoformat(), "axes": DAY_AXES,
            "window": int(MATCH_WINDOW.total_seconds()), "events": events, "series": series}


def draw_candidates(ax, points, color, *, ambiguous=False):
    if not points:
        return
    times, values = zip(*points)
    if ambiguous:
        ax.scatter(times, values, marker="x", s=12, color=color, linewidths=.8, zorder=5)
    else:
        ax.scatter(times, values, marker="o", s=18, facecolors="none", edgecolors=color, linewidths=.9, zorder=6)


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
    packets, continuous = detailed_packets(folder, start - MATCH_WINDOW, end + MATCH_WINDOW, tags)
    exact = {tag: packet_gaps(packets[tag], start, end) for tag in tags}
    metadata = inventory(root)
    fig = Figure(figsize=(13, 9.5 + .16 * max(0, len(tags) - 4)))
    hits, gaps, strength, temp, findmy = axes = fig.subplots(
        5, 1, sharex=True, gridspec_kw={"height_ratios": [1.4, 1.15, 1.3, 1.2, 1 + .21 * max(0, len(tags) - 4)]}
    )
    fig.subplots_adjust(**DAY_AXES, hspace=.56)
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
        counts = minute_counts(local[tag], start)
        if counts:
            hits.step(bins, [counts[i] for i in range(len(bins))], where="post", color=color, lw=1.65)
        draw_trace(gaps, sorted(exact[tag]), color)
        rssi, temps = minute_traces(local[tag], metadata.get(tag, {}))
        for ax, points in ((strength, rssi), (temp, temps)):
            draw_trace(ax, points, color)
            draw_smooth(ax, points, color)
        matched = list(findmy_matches(reports[tag], packets[tag], continuous[tag]))
        unique = {items[0] for _, items in matched if len(items) == 1}
        ambiguous = {p for _, items in matched if len(items) > 1 for p in items} - unique
        for group, multiple in ((unique, False), (ambiguous, True)):
            draw_candidates(strength, sorted((p.when, p.rssi) for p in group if p.rssi is not None),
                            color, ambiguous=multiple)
            draw_candidates(gaps, sorted((p.when, p.gap) for p in group if p.gap and p.gap > 0),
                            color, ambiguous=multiple)
        times = [when for when, _ in matched]
        findmy.axhline(lane, color=grid, lw=.8)
        findmy.get_yticklabels()[lane].set(color=color if counts or times else muted, weight="bold")
        if times:
            findmy.scatter(times, [lane] * len(times), marker="|", s=20, color=color, alpha=.56, linewidths=.85, zorder=3)
            draw_candidates(findmy, [(when, lane) for when, items in matched if len(items) == 1], color)
            draw_candidates(findmy, [(when, lane) for when, items in matched if len(items) > 1], color, ambiguous=True)
        else:
            findmy.text(.013, lane, "No Find My observations", va="center", color=muted, size=9,
                        transform=findmy.get_yaxis_transform(),
                        bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5})
    guides = sorted({when for tag in tags for when in reports[tag]})
    for ax in axes:
        if guides:
            ax.vlines(guides, 0, 1, transform=ax.get_xaxis_transform(),
                      color=muted, linewidth=.65, alpha=.16, zorder=2)
    if end < day_end:
        for ax in axes:
            ax.axvspan(end, day_end, color="#F1F4F7", zorder=0)
    findmy.set_xlim(start, day_end)
    findmy.xaxis.set_major_locator(dates.AutoDateLocator(tz=start.tzinfo, minticks=5, maxticks=13))
    findmy.xaxis.set_major_formatter(dates.DateFormatter("%H:%M", tz=start.tzinfo))
    fig.text(.125, .042, "Faint guides mark Find My observation times. Rings mark one Pi candidate and crosses mark multiple.",
             color=muted, size=9)
    fig.text(.125, .024, "Candidates are within ±10 s for the same tag. Status bytes must agree when both are available.",
             color=muted, size=8.5)

    path = day_plot_path(folder, day, selected)
    with BytesIO() as image:
        fig.savefig(image, format="png", dpi=160, backend="agg")
        write_private(path, image.getvalue())
    return path
