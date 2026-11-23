# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a compact health timeline on the BLE day plot's time axis."""

import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from . import diagnostics
from .ble_day import day_bounds, day_plot_path, findmy_sources
from .ble_stream import resolve, short_name
from .settings import DATA, write_private

MINUTE = timedelta(minutes=1)
HEARTBEAT = "#167854"
JOURNAL = "#A5BACB"
POLL = {
    "polling": ("|", "#2268AF"),
    "ok": ("o", "#167854"),
    "backoff": ("x", "#A93E25"),
    "needs_login": ("x", "#A93E25"),
    "saved": ("D", "#88589E"),
}


def service_spans(events, start, end):
    spans = defaultdict(list)
    running = {}
    cutoff = min(end, max((t for t, source, _, _ in events if source == "journal"), default=start))
    for when, source, unit, state in events:
        if source != "systemd" or when >= cutoff:
            continue
        lane = "monitor" if unit == diagnostics.MONITOR_UNIT else "recorder"
        if unit in running:
            began = max(start, running.pop(unit))
            if began < when:
                spans[lane].append((began, when))
        if state == "start":
            running[unit] = when
    for unit, when in running.items():
        began = max(start, when)
        if began < cutoff:
            lane = "monitor" if unit == diagnostics.MONITOR_UNIT else "recorder"
            spans[lane].append((began, cutoff))
    return spans


def heartbeat_spans(times, end):
    if not times:
        return []
    spans = []
    first = last = times[0]
    for when in times[1:]:
        if when - last > MINUTE:
            spans.append((first, min(end, last + MINUTE)))
            first = when
        last = when
    spans.append((first, min(end, last + MINUTE)))
    return spans


def saved_report_times(root, start, end, selected):
    if start >= end:
        return {}
    saved = defaultdict(set)
    bounds = tuple(diagnostics.stamp(t) for t in (start, end))
    for tag, path in findmy_sources(root):
        if selected and tag not in selected:
            continue
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            for (minute,) in db.execute("""SELECT DISTINCT substr(first_fetched_at, 1, 16)
                FROM sightings WHERE first_fetched_at >= ? AND first_fetched_at < ?""", bounds):
                saved[tag].add(datetime.fromisoformat(minute + "+00:00").astimezone(start.tzinfo))
    return saved


def render_diagnostics(day, selected=(), root=DATA, folder=None):
    from matplotlib import dates
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D

    folder = folder if folder is not None else Path.home() / "blelog"
    start, day_end = day_bounds(day)
    now = datetime.now().astimezone(start.tzinfo)
    if now < start:
        raise ValueError(f"{day} is in the future")
    end = min(day_end, now)
    events = diagnostics.read(root, start, end)
    service = service_spans(events, start, end)
    ticks = defaultdict(list)
    polls = defaultdict(lambda: defaultdict(set))
    first_poll = min((t for t, source, _, _ in events if source == "findmy"), default=end)
    for when, source, subject, state in events:
        if state == "heartbeat":
            ticks[source].append(when)
        elif source == "findmy":
            tag = resolve(subject)
            if not selected or tag in selected:
                polls[tag if selected else None][state].add(when)
    for tag, times in saved_report_times(root, start, first_poll, selected).items():
        polls[tag if selected else None]["saved"].update(times)

    lanes = ["Pi BLE monitor", "Packet recorder"]
    lanes.extend(f"Find My · {short_name(tag)}" for tag in selected)
    if not selected:
        lanes.append("Find My")
    fig = Figure(figsize=(13, 2.4 + .32 * len(lanes)))
    ax = fig.subplots()
    fig.subplots_adjust(left=.125, right=.965, top=.79, bottom=.31)
    ink, muted, grid = "#192638", "#46566D", "#E1E6EC"
    fig.text(.125, .94, "Recorder and polling diagnostics", color=ink, size=17, weight="bold", va="top")
    fig.text(.125, .842, f"{start:%d %b %Y}  ·  through {end:%H:%M} {end:%Z}",
             color=muted, size=9)
    for lane in range(len(lanes)):
        ax.axhline(lane, color=grid, lw=.9, zorder=1)
    for lane, source in enumerate(("monitor", "recorder")):
        for color, spans, zorder in ((JOURNAL, service[source], 2),
                                      (HEARTBEAT, heartbeat_spans(ticks[source], end), 3)):
            for began, finished in spans:
                ax.hlines(lane, began, finished, color=color, lw=5, zorder=zorder)
    for lane, tag in enumerate(selected or (None,), 2):
        for state, times in polls[tag].items():
            marker, color = POLL[state]
            ax.scatter(sorted(times), [lane] * len(times), marker=marker, s=29 if marker in "|x" else 14,
                       color=color, linewidths=1.1, zorder=4)
    if end < day_end:
        ax.axvspan(end, day_end, color="#F1F4F7", zorder=0)
    ax.set_xlim(start, day_end)
    ax.set_yticks(range(len(lanes)), lanes, color=muted)
    ax.set_ylim(len(lanes) - .4, -.6)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.tick_params(length=0, pad=6, labelcolor=muted)
    ax.grid(axis="x", color=grid, linewidth=.7)
    ax.xaxis.set_major_locator(dates.AutoDateLocator(tz=start.tzinfo, minticks=5, maxticks=13))
    ax.xaxis.set_major_formatter(dates.DateFormatter("%H:%M", tz=start.tzinfo))
    handles = [
        Line2D([], [], color=HEARTBEAT, lw=4, label="Pi heartbeat"),
        Line2D([], [], color=JOURNAL, lw=4, label="Service journal"),
        *(Line2D([], [], marker=POLL[key][0], color=POLL[key][1], linestyle="None", label=label)
          for key, label in (("polling", "Poll started"), ("ok", "Poll succeeded"),
                             ("backoff", "Backoff or sign-in required"), ("saved", "Report saved (older history)"))),
    ]
    fig.legend(handles=handles, ncol=3, loc="lower left", bbox_to_anchor=(.125, .085),
               frameon=False, fontsize=8.5, borderaxespad=0)
    fig.text(.125, .034, "Blank portions have no recorded heartbeat, running interval, or poll event.",
             color=muted, size=8.5)
    path = day_plot_path(folder, day, selected, diagnostics=True)
    with BytesIO() as image:
        fig.savefig(image, format="png", dpi=160, backend="agg")
        write_private(path, image.getvalue())
    return path
