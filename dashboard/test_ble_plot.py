# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from .ble_plot import read_packets


class PacketPlotTests(unittest.TestCase):
    def test_recordings_from_before_interval_column_recover_gaps_per_network(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.tsv"
            path.write_text(
                "timestamp\ttag\tnetwork\trssi_dbm\ttemp_c\traw_ad_hex\n"
                "2026-11-22T17:00:00+01:00\th03\tfindmy\t-65\t8\t00\n"
                "2026-11-22T17:00:01+01:00\th03\thubble\t-67\t8\t00\n"
                "2026-11-22T17:00:03+01:00\th03\tfindmy\t-66\t8\t00\n"
                "2026-11-22T17:00:05+01:00\th03\thubble\t-68\t8\t00\n",
                encoding="utf-8",
            )
            self.assertEqual([packet[1] for packet in read_packets(path)["h03"]], [None, None, 3.0, 4.0])
