# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep one write-once OTP record per board and serve private programming files."""

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ble import reception_history
from .settings import DATA

TAG = re.compile(r"(?:Hagalu|Irulu)-[0-9]{3,}")
FILE = re.compile(r"(hagalu|irulu)_([0-9]{3,})\.[A-Za-z0-9_.-]+\.(?:cfg|key)|(?:hagalu|irulu)_[0-9]{3,}\.keys")
NETWORK = {"findmy": "Find My", "hubble": "Hubble", "dual": "Find My + Hubble"}
BOARDS = ("S6", "S6L", "N6", "V1", "V2", "unknown")


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def same_file(path, contents):
    if path.read_bytes() != contents:
        raise ValueError(f"{path.name} already exists with different contents")


def write_once(path, contents, *, staging=None):
    """Publish a complete file atomically, accepting an identical retry."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=staging or path.parent) as temp:
        temp.write(contents)
        temp.flush()
        os.fchmod(temp.fileno(), 0o400)
        os.fsync(temp.fileno())
        try:
            os.link(temp.name, path)
        except FileExistsError:
            same_file(path, contents)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def record(root, entry, config=None):
    tag = entry["tag"]
    if not TAG.fullmatch(tag) or entry["board"] not in BOARDS or entry["network"] not in NETWORK:
        raise ValueError("Invalid tag, board or network")
    datetime.fromisoformat(entry["burned_at"])
    entry = dict(entry)
    if config is not None:
        image = config.read_bytes()
        json.loads(image)
        digest = hashlib.sha256(image).hexdigest()
        if entry.get("config_sha256") not in (None, digest):
            raise ValueError("Programmer output does not match the recorded SHA-256")
        entry["config_sha256"] = digest
    if entry.get("config_sha256") and not re.fullmatch(r"[a-f0-9]{64}", entry["config_sha256"]):
        raise ValueError("Invalid SHA-256")
    ledger = root / "flash-history" / f"{tag}.json"
    contents = (json.dumps(entry, indent=2, sort_keys=True) + "\n").encode()
    if ledger.exists():
        same_file(ledger, contents)
    if config is not None:
        write_once(archived_config(root, tag), image)
    write_once(ledger, contents, staging=root)
    return entry


def archived_config(root, tag):
    return root / "secrets/flash-artifacts" / tag / "programmed.cfg"


def records(root):
    return {path.stem: read_json(path, {}) for path in sorted((root / "flash-history").glob("*.json"))
            if TAG.fullmatch(path.stem)}


def private_files(root):
    files = {}
    for path in sorted((root / "secrets/tag-keys").glob("*")):
        if path.is_file() and not path.is_symlink() and FILE.fullmatch(path.name) and "RAMONLY" not in path.name.upper():
            tag = path.name.split(".", 1)[0].replace("_", "-").capitalize()
            files.setdefault(tag, []).append(path)
    return files


def download(root, name):
    if not FILE.fullmatch(name):
        return None
    tag = name.split(".", 1)[0].replace("_", "-").capitalize()
    if name.endswith(".programmed.cfg"):
        entry = read_json(root / "flash-history" / f"{tag}.json", {})
        artifact = archived_config(root, tag)
        if artifact.is_file() and not artifact.is_symlink():
            contents = artifact.read_bytes()
            if hashlib.sha256(contents).hexdigest() == entry.get("config_sha256"):
                return contents
        return None
    for path in private_files(root).get(tag, []):
        if path.name == name:
            return path.read_bytes()
    return None


def local_time(value):
    if not value:
        return "—"
    if len(value) == 10:
        return html.escape(value)
    return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y, %H:%M:%S %Z")


def link(name, label):
    return f'<a href="/flash/files/{html.escape(name)}" download>{html.escape(label)}</a>'


def row(root, tag, entry, planned, seen, files):
    name = html.escape(tag)
    flashed = bool(entry)
    configs = [p for p in files if p.suffix == ".cfg"
               and (".combo" not in p.name or p.name == planned.get("bench_config"))]
    config_name = entry.get("config_name")
    if flashed:
        configs = []
        if config_name:
            configs = [p for p in files if p.name == config_name]
    links = [link(p.name, p.name.split(".", 1)[1]) for p in configs]
    programmed = archived_config(root, tag)
    if flashed and programmed.is_file():
        links.insert(0, link(tag.lower().replace("-", "_") + ".programmed.cfg", "Programmer output"))
    keys = [link(p.name, "Find My key" if p.suffix == ".keys" else "Hubble key")
            for p in files if p.suffix in (".key", ".keys")]
    last = local_time(seen.get("last_seen"))
    if seen:
        detail = ""
        if seen.get("rssi") is not None:
            detail = f"{int(seen['rssi'])} dBm"
        if entry.get("telemetry") == "findmy-temperature" and seen.get("status_byte") is not None:
            status = int(seen["status_byte"])
            detail = " · ".join(filter(None, (detail, f"{(status if status < 128 else status - 256) / 2:g} °C")))
        if detail:
            last += f"<small>{detail}</small>"
    sha = entry.get("config_sha256")
    if flashed:
        program = f"OTP · {local_time(entry['burned_at'])}"
        details = html.escape(entry.get("evidence", ""))
        if sha:
            details += f'<code title="{html.escape(sha)}">{html.escape(sha)}</code>'
        program += f"<small>{details}</small>"
    else:
        program = "Prepared" if configs else "Awaiting config"
        if planned.get("bench_config"):
            program += "<small>RAM-tested · OTP unconfirmed</small>"
    board = entry.get("board", planned.get("board", "unknown"))
    network = NETWORK.get(entry.get("network", planned.get("network")), "—")
    files_html = "<br>".join(links + keys) or "—"
    return (f'<tr><th scope="row">{name}</th><td>{html.escape(board)} · {network}</td>'
            f'<td>{program}</td><td>{last}</td><td>{files_html}</td></tr>')


def page(root):
    ledger = records(root)
    plan = read_json(root / "flash-plan.json", {})
    live = read_json(root / "ble-live.json", {})
    captures = read_json(root / "ble-captures.json", [])
    seen = live.get("tags", {})
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()[:16]
    for tag, _minute, count, last_seen, rssi, status_byte in reception_history(root, since):
        if tag not in seen:
            seen[tag] = {"last_seen": last_seen, "reports": count, "rssi": rssi, "status_byte": status_byte}
    for capture in sorted(captures, key=lambda item: item["ended_at"] or "", reverse=True):
        for tag, item in capture["tags"].items():
            if tag not in seen:
                seen[tag] = item
    last_scan = live.get("updated_at")
    active = last_scan and (datetime.now().astimezone() - datetime.fromisoformat(last_scan)).total_seconds() < 45
    files = private_files(root)
    tags = sorted(ledger.keys() | plan.keys() | files.keys() | seen.keys())
    rows = "".join(row(root, tag, ledger.get(tag, {}), plan.get(tag, {}), seen.get(tag, {}), files.get(tag, []))
                   for tag in tags)
    capture_rows = []
    for capture in captures:
        matches = ", ".join(f"{html.escape(tag)} ({int(item['reports'])})" for tag, item in capture["tags"].items())
        capture_rows.append(f"<li><strong>{html.escape(capture['file'])}</strong><small>"
                            f"{local_time(capture['started_at'])} to {local_time(capture['ended_at'])} · "
                            f"{matches or 'No registered tags matched'}</small>"
                            f"<small>{html.escape(capture.get('note', ''))}</small></li>")
    table = ('<div class="table-wrap" role="region" aria-label="Flash records" tabindex="0"><table><thead><tr>'
             '<th scope="col">Tag</th><th scope="col">Board and network</th><th scope="col">Flash record</th>'
             '<th scope="col">Last Pi BLE reception</th><th scope="col">Private files</th></tr></thead>'
             f"<tbody>{rows}</tbody></table></div>") if rows else "<p>No tags are recorded on this host yet.</p>"
    archives = ("<section><h2>Saved BLE captures</h2><ul>" + "".join(capture_rows) + "</ul></section>") if captures else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flash history · Gantumoote</title><link rel="stylesheet" href="/assets/flash.css"></head>
<body><main><nav aria-label="Dashboard"><a href="/">← Gantumoote</a> <a href="/flash">Refresh</a></nav>
<h1>Flash history</h1>
<p class="status {'active' if active else ''}">{'Pi BLE monitor active' if active else 'Pi BLE monitor unavailable'}
{' · ' + local_time(last_scan) if last_scan else ''}</p>
<p>Match the board marking and tag number before downloading its prepared configuration. Use the same tag’s key
 to register it. The download stays private under this dashboard session.</p>
{table}
<p><a href="/flash/ble-day">View daily BLE and Find My plots</a></p>
<p><a href="/flash/ble.csv" download>Download the last 24 hours of Pi BLE receptions</a></p>{archives}
</main></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("board", choices=BOARDS)
    parser.add_argument("network", choices=NETWORK)
    parser.add_argument("burned_at", help="ISO date or timestamp with offset")
    parser.add_argument("--config", type=Path, required=True, help="exact NanoBeacon programmer output")
    parser.add_argument("--source", help="prepared config's filename")
    parser.add_argument("--temperature", action="store_true", help="Find My status byte encodes temperature")
    args = parser.parse_args()
    entry = {"tag": args.tag, "board": args.board, "network": args.network, "burned_at": args.burned_at,
             "evidence": "Exact NanoBeacon programmer output", "config_name": args.source}
    if args.temperature:
        entry["telemetry"] = "findmy-temperature"
    try:
        entry = record(DATA, entry, args.config)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"{entry['tag']} · {entry['config_sha256']}")


if __name__ == "__main__":
    main()
