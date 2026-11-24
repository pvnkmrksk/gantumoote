# SPDX-License-Identifier: AGPL-3.0-or-later
"""Summarize same-day Pi BLE, Find My, temperature, and optional Phyphox lux."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from statistics import StatisticsError, correlation, median

from .ble_day import day_data, detailed_packets, findmy_matches, findmy_sources, minute_counts
from .ble_plot import read_light
from .ble_stream import resolve
from .diagnostics import read as diagnostic_events
from .flash import TAG
from .settings import DATA

WINDOWS = (5, 15, 30)
BLOCK = timedelta(minutes=5)


def bucket(when, start, minutes=5):
    return int((when - start).total_seconds() // (minutes * 60))


def coverage(ble, findmy):
    return {"hit": len(ble & findmy), "bins": len(ble), "findmy_only": len(findmy - ble)}


def ranks(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        for index in order[i:j]:
            result[index] = (i + j - 1) / 2
        i = j
    return result


def association(a, b):
    keys = sorted(a.keys() & b.keys())
    try:
        rho = correlation(ranks([a[key] for key in keys]), ranks([b[key] for key in keys])) if len(keys) >= 3 else None
    except StatisticsError:
        rho = None
    return {"n": len(keys), "rho": round(rho, 3) if rho is not None else None}


def consecutive_differences(values):
    return {i: value - values[i - 1] for i, value in values.items() if i - 1 in values}


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * p
    low = int(index)
    return round(values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low), 3)


def distribution(values):
    return {
        "n": len(values), "min": min(values, default=None), "p10": percentile(values, .1),
        "p50": percentile(values, .5), "p90": percentile(values, .9), "max": max(values, default=None),
    }


def timestamp(when):
    return when.isoformat(timespec="milliseconds") if when is not None else None


def peak(counts, start, width=1):
    if not counts:
        return None
    keep = min(range(min(counts), max(counts) + 1),
               key=lambda i: (-sum(counts.get(j, 0) for j in range(i, i + width)), i))
    return {"start": timestamp(start + keep * BLOCK), "packets": sum(counts.get(i, 0) for i in range(keep, keep + width))}


def fix_quality(root, start, end, tags, comparison):
    fixes = defaultdict(list)
    bounds = tuple(when.astimezone(timezone.utc).isoformat() for when in (start, end))
    for tag, path in findmy_sources(root):
        if tag not in tags:
            continue
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            fixes[tag].extend(db.execute("""SELECT latitude,longitude,accuracy_m FROM sightings
                WHERE timestamp >= ? AND timestamp < ?""", bounds))
    common = [fix for tag in comparison for fix in fixes[tag]]
    center = (median(fix[0] for fix in common), median(fix[1] for fix in common)) if common else None

    def distance(lat, lon):
        lat0, lon0 = center
        dlat, dlon = radians(lat - lat0), radians(lon - lon0)
        a = sin(dlat / 2) ** 2 + cos(radians(lat0)) * cos(radians(lat)) * sin(dlon / 2) ** 2
        return 12_742_000 * asin(sqrt(min(1, a)))

    return {tag: {
        "reported_accuracy_m": distribution([row[2] for row in fixes[tag]]),
        "distance_from_pooled_median_m": distribution([distance(*row[:2]) for row in fixes[tag]])
        if center and tag in comparison else None,
    } for tag in tags}


def analyze(day, tags, comparison=(), lux_files=(), *, root=DATA, folder=None):
    folder = folder if folder is not None else Path.home() / "blelog"
    start, end, local, reports = day_data(day, root)
    through = min(end, datetime.now().astimezone(start.tzinfo))
    details, continuous = detailed_packets(folder, start, through, tags)
    packets_by_tag = {tag: continuous[tag] or details[tag] for tag in tags}
    all_counts = {tag: minute_counts(local[tag], start) for tag in tags}
    lux_points = defaultdict(list)
    for path in lux_files:
        for when, value in read_light(path):
            if start <= when < through:
                lux_points[bucket(when, start)].append(value)
    lux = {i: median(values) for i, values in lux_points.items()}
    active_bins = {}
    report_bins = {}
    results = {}
    for tag in tags:
        readings = sorted(packets_by_tag[tag], key=lambda packet: packet.when)
        seen = sorted(reports[tag])
        temps = defaultdict(list)
        for packet in readings:
            if packet.temp is not None:
                temps[bucket(packet.when, start)].append(packet.temp)
        med_temp = {i: median(values) for i, values in temps.items()}
        rates = dict(all_counts[tag])
        complete = {width: int((through - start).total_seconds() // (width * 60)) for width in WINDOWS}
        active_bins[tag] = {width: {bucket(row[0], start, width) for row in local[tag]
                                    if bucket(row[0], start, width) < complete[width]} for width in WINDOWS}
        report_bins[tag] = {width: {bucket(when, start, width) for when in seen
                                    if bucket(when, start, width) < complete[width]} for width in WINDOWS}
        matches = list(findmy_matches(reports[tag], details[tag], continuous[tag]))
        counts = Counter(len(items) for _, items in matches)
        unique_offsets = [abs((items[0].when - when).total_seconds()) for when, items in matches if len(items) == 1]
        rssi = [packet.rssi for packet in readings if packet.rssi is not None]
        intervals = [packet.gap for packet in readings if packet.gap is not None]
        max_temp = max(med_temp.values(), default=None)
        warmest = [i for i, value in med_temp.items() if value == max_temp]
        hourly = {}
        for i in sorted({i // 12 for i in rates}):
            hour = start + timedelta(hours=i)
            batch = range(i * 12, (i + 1) * 12)
            hourly[hour.strftime("%H:00")] = {
                "ble": sum(rates.get(j, 0) for j in batch),
                "findmy": sum(bucket(when, start, 60) == i for when in seen),
                "temp": median(med_temp[j] for j in batch if j in med_temp) if any(j in med_temp for j in batch) else None,
            }
        results[tag] = {
            "ble_count": sum(rates.values()), "raw_count": len(readings),
            "first_ble": timestamp(readings[0].when if readings else None),
            "last_ble": timestamp(readings[-1].when if readings else None),
            "first_findmy": timestamp(seen[0] if seen else None), "last_findmy": timestamp(seen[-1] if seen else None),
            "findmy_reports": len(seen),
            "findmy_gap_s": distribution([(b - a).total_seconds() for a, b in zip(seen, seen[1:])]),
            "coverage": {width: coverage(active_bins[tag][width], report_bins[tag][width]) for width in WINDOWS},
            "timing_candidates": {"none": counts[0], "one": counts[1], "multiple": sum(n for k, n in counts.items() if k > 1),
                                  "single_abs_offset_median_s": percentile(unique_offsets, .5)},
            "interval_s": distribution(intervals), "rssi_dbm": distribution(rssi),
            "temp_c": distribution([packet.temp for packet in readings if packet.temp is not None]),
            "peak_5min": peak(rates, start), "peak_30min": peak(rates, start, 6),
            "warmest_5min": {
                "temp_c": max_temp, "first": timestamp(start + min(warmest) * BLOCK),
                "last": timestamp(start + max(warmest) * BLOCK),
            } if warmest else None,
            "temp_vs_receptions": association(med_temp, rates),
            "temp_change_vs_reception_change": association(consecutive_differences(med_temp), consecutive_differences(rates)),
            "lux_vs_receptions": association(lux, rates) if lux else None,
            "lux_vs_temp": association(lux, med_temp) if lux else None,
            "hourly": hourly,
        }

    common = {width: set.intersection(*(active_bins[tag][width] for tag in comparison)) if comparison else set()
              for width in WINDOWS}
    combined = Counter()
    for tag in comparison:
        combined.update(all_counts[tag])
        results[tag]["common_coverage"] = {width: coverage(common[width], report_bins[tag][width]) for width in WINDOWS}
    ranked = sorted(common[5], key=lambda i: (combined[i], i))
    if ranked:
        quiet, busy = set(ranked[:len(ranked) // 2]), set(ranked[len(ranked) // 2:])
        for tag in comparison:
            results[tag]["quiet_common_5min"] = coverage(quiet, report_bins[tag][5])
            results[tag]["busy_common_5min"] = coverage(busy, report_bins[tag][5])

    for tag, quality in fix_quality(root, start, through, tags, comparison).items():
        results[tag]["fix_quality"] = quality
    health = diagnostic_events(root, start, through)
    return {
        "day": day.isoformat(), "through": timestamp(through), "lux_bins": len(lux),
        "heartbeat_minutes": dict(Counter(source for _, source, _, state in health if state == "heartbeat")),
        "findmy_poll_states": dict(Counter(state for _, source, _, state in health if source == "findmy")),
        "comparison": list(comparison), "comparison_peak_5min": peak(combined, start),
        "comparison_peak_30min": peak(combined, start, 6), "tags": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", type=date.fromisoformat)
    parser.add_argument("--tags", nargs="+", required=True, help="tag names or aliases")
    parser.add_argument("--compare", nargs="+", default=(), help="tags known to share a setup")
    parser.add_argument("--lux", type=Path, action="append", default=[], help="a saved Phyphox .lux.tsv from the same setup")
    args = parser.parse_args()
    tags = tuple(dict.fromkeys(resolve(name) for name in args.tags))
    comparison = tuple(dict.fromkeys(resolve(name) for name in args.compare))
    if any(not TAG.fullmatch(tag) for tag in tags) or not set(comparison) <= set(tags):
        parser.error("Use tag names such as h03. Comparison tags must also be in --tags.")
    print(json.dumps(analyze(args.day, tags, comparison, args.lux), indent=2))


if __name__ == "__main__":
    main()
