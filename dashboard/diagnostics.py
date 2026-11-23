# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private timestamps for Pi scan health, Find My polling, and retained journal events."""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

from .settings import DATA

DATABASE = "ble-diagnostics.sqlite3"
POLL_STATES = ("polling", "ok", "backoff", "needs_login")
MONITOR_UNIT = "gantumoote-ble.service"
RECORDERS = re.compile(r"gantumoote-(?:ble-recording|hagalu-recording|ble-overnight[-a-z0-9]*)\.service")
UNIT_STATES = {
    "39f53479d3a045ac8e11786248231fbf": "start",
    "de5b426a63be47a7b6ac3eaac82e2f6f": "stop",
    "7ad2d189f7e94e70a38c781354912448": "stop",
    "9d1aaa27d60140bd96365438aad20286": "stop",
    "98e322203f7a4ed290d09fe03c09fe15": "stop",
    "d9b373ed55a64feb8242e02dbe79a49c": "stop",
}


def stamp(when):
    return when.astimezone(timezone.utc).isoformat()


def record(root, events):
    path = root / "secrets" / DATABASE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            at_utc TEXT, source TEXT, subject TEXT, state TEXT,
            PRIMARY KEY (at_utc, source, subject, state)
        )""")
        db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)", events)


def record_poll(root, state, tags):
    if not tags:
        return
    when = stamp(datetime.now(timezone.utc))
    try:
        record(root, ((when, "findmy", tag, state) for tag in tags))
    except (OSError, sqlite3.Error):
        logging.warning("Could not save Find My polling diagnostics.")


class Heartbeat:
    def __init__(self, root, source):
        self.root = root
        self.source = source
        self.minute = None

    def tick(self):
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if minute == self.minute:
            return
        self.minute = minute
        try:
            record(self.root, [(stamp(minute), self.source, "", "heartbeat")])
        except (OSError, sqlite3.Error):
            logging.warning("Could not save %s heartbeat.", self.source)


def read(root, start, end):
    path = root / "secrets" / DATABASE
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events' AND type='table'").fetchone():
            return []
        rows = db.execute("""SELECT at_utc, source, subject, state FROM events
            WHERE (at_utc >= ? AND at_utc < ?) OR source IN ('systemd', 'journal')
            ORDER BY at_utc""", (stamp(start), stamp(end))).fetchall()
    return [(datetime.fromisoformat(when).astimezone(start.tzinfo), source, subject, state)
            for when, source, subject, state in rows]


def journal_events(lines):
    for line in lines:
        row = json.loads(line)
        unit = row.get("UNIT", "")
        state = UNIT_STATES.get(row.get("MESSAGE_ID"))
        if state and (unit == MONITOR_UNIT or RECORDERS.fullmatch(unit)):
            when = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc)
            yield stamp(when), "systemd", unit, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-journal", action="store_true", required=True, help="read systemd JSON lines from stdin")
    parser.parse_args()
    events = list(journal_events(sys.stdin))
    if events:
        record(DATA, [*events, (stamp(datetime.now(timezone.utc)), "journal", "", "snapshot")])
    print(f"Imported {len(events)} recorder and monitor service events.")


if __name__ == "__main__":
    main()
