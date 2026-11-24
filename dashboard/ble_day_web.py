# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authenticated dashboard view of the existing BLE day plot."""

import html
import re
import subprocess
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from .ble_day import day_plot_path
from .ble_stream import resolve
from .flash import TAG
from .settings import PROJECT

PAGE = "/flash/ble-day"
IMAGE = f"{PAGE}.png"
DIAGNOSTICS = f"{PAGE}-diagnostics.png"
MATCHES = f"{PAGE}-matches.csv"
EVENTS = f"{PAGE}-events.json"
CHART_HEADERS = {"Cross-Origin-Resource-Policy": "same-origin"}
PAGE_HEADERS = {
    **CHART_HEADERS,
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}
_RENDER_SLOT = threading.BoundedSemaphore()


def selection(query, today):
    options = parse_qs(query, keep_blank_values=True, max_num_fields=4)
    day = date.fromisoformat(options.get("date", [today.isoformat()])[0])
    if day > today:
        raise ValueError("Choose today or an earlier date.")
    tags = tuple(dict.fromkeys(resolve(name) for name in re.findall(r"[^,\s]+", options.get("tags", [""])[0])))
    if len(tags) > 12 or any(not TAG.fullmatch(tag) for tag in tags):
        raise ValueError("Enter up to 12 tag names, for example h03 h04.")
    return day, tags


def page(day, tags, today):
    tag_value = " ".join(tags)
    query = urlencode({"date": day.isoformat(), "tags": tag_value})
    image = html.escape(f"{IMAGE}?{query}", quote=True)
    diagnostics = html.escape(f"{DIAGNOSTICS}?{query}", quote=True)
    matches = html.escape(f"{MATCHES}?{query}", quote=True)
    events = html.escape(f"{EVENTS}?{query}", quote=True)
    tag_value = html.escape(tag_value, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily BLE plots · Gantumoote</title><link rel="stylesheet" href="/assets/flash.css">
<script src="/assets/ble-day.js" defer></script></head>
<body><main><nav aria-label="Dashboard"><a href="/flash">← Flash history &amp; Pi BLE</a><a href="/">Map</a></nav>
<h1>BLE and Find My by day</h1>
<form class="day-controls" action="{PAGE}" method="get">
  <label>Date <input name="date" type="date" required value="{day}" max="{today}"></label>
  <label>Tags <input name="tags" type="text" maxlength="160" placeholder="All tags heard or reported" value="{tag_value}"></label>
  <button type="submit">Show plot</button>
</form>
<p>Times use the Pi’s clock. <span class="day-report-hint" hidden>Hover or tap a Find My guide to inspect nearby adverts.</span></p>
<figure class="day-plot">
  <div class="day-plot-frame" data-report-events="{events}">
    <a href="{image}"><img src="{image}" alt="Daily BLE reception counts, exact packet gaps where available, RSSI, temperature, and Find My timestamps for {day}"></a>
    <div class="day-plot-target" hidden aria-hidden="true">
      <span class="day-report-cursor" hidden></span><span class="day-report-tooltip" hidden></span>
    </div>
  </div>
  <figcaption><a href="{image}" download>Download PNG</a> · <a href="{matches}" download>Advert matches CSV</a></figcaption>
</figure>
<section class="day-report-inspector" aria-label="Inspect Find My reports" hidden>
  <label>Find My report <select></select></label>
  <div class="day-report-result" aria-live="polite"></div>
</section>
<details class="day-diagnostics">
  <summary>Diagnostics</summary>
  <p>Compare the Pi’s scan and recorder activity with Find My polling on the same timeline.
  Older service activity comes from the Pi journal. Older Find My marks show when reports were saved.</p>
  <figure class="day-plot">
    <a href="{diagnostics}"><img src="{diagnostics}" loading="lazy" decoding="async"
      alt="Pi monitor and recorder uptime, Find My poll attempts and results, and older saved reports for {day}"></a>
    <figcaption><a href="{diagnostics}" download>Download diagnostics PNG</a></figcaption>
  </figure>
</details>
</main></body></html>"""


def plot(day, tags, *, diagnostics=False):
    path = day_plot_path(Path.home() / "blelog", day, tags, diagnostics=diagnostics)

    def fresh():
        try:
            return time.time() - path.stat().st_mtime < 60
        except FileNotFoundError:
            return False

    if fresh():
        return path.read_bytes()
    if not _RENDER_SLOT.acquire(timeout=15):
        raise TimeoutError("A day plot is already being rendered")
    try:
        if not fresh():
            # The Pi installs Matplotlib in its system Python for the BLE tools.
            command = ["/usr/bin/python3", "-m", "dashboard.ble_stream", "--plot-day", day.isoformat()]
            if diagnostics:
                command.append("--diagnostics")
            if tags:
                command.extend(("--tags", *tags))
            subprocess.run(command, cwd=PROJECT, check=True, timeout=30, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        return path.read_bytes()
    finally:
        _RENDER_SLOT.release()
