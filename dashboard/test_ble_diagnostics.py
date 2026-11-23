# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .ble_day_health import service_spans
from .diagnostics import MONITOR_UNIT, journal_events, read, record, stamp


class ServiceHistoryTests(unittest.TestCase):
    def test_journal_preserves_stop_and_restart_within_one_second(self):
        start = datetime(2026, 11, 23, tzinfo=timezone.utc)
        stop = start + timedelta(seconds=1, microseconds=100)
        restart = stop + timedelta(microseconds=100)
        end = start + timedelta(hours=1)
        started = "39f53479d3a045ac8e11786248231fbf"
        stopped = "de5b426a63be47a7b6ac3eaac82e2f6f"
        lines = [json.dumps({"UNIT": MONITOR_UNIT, "MESSAGE_ID": state,
                             "__REALTIME_TIMESTAMP": round(when.timestamp() * 1e6)})
                 for when, state in ((start, started), (stop, stopped), (restart, started))]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record(root, [*journal_events(lines), (stamp(end), "journal", "", "snapshot")])
            spans = service_spans(read(root, start, end), start, end)
        self.assertEqual(spans["monitor"], [(start, stop), (restart, end)])
