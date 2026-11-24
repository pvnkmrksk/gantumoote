# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest
from datetime import datetime, timedelta, timezone

from .ble_day import findmy_matches
from .ble_plot import Packet, findmy_status


class FindMyCandidateTests(unittest.TestCase):
    def test_candidate_window_tag_network_and_status(self):
        then = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)

        def packet(seconds, status=16, network="findmy"):
            return Packet(then + timedelta(seconds=seconds), 30, -66, 8, network, status)

        one, two = packet(-10), packet(3)
        rows = [packet(-10.001), one, packet(0, 17), packet(1, network="hubble"), two, packet(10.001)]
        self.assertEqual(list(findmy_matches({then: {16}}, rows)), [(then, (one, two))])
        self.assertEqual(list(findmy_matches({then: {16}}, rows, [two])), [(then, (two,))])
        self.assertEqual(list(findmy_matches({then: {17}}, [one, two])), [(then, ())])

    def test_status_is_the_find_my_advert_byte(self):
        self.assertEqual(findmy_status("1eff4c0012191000"), 16)
        self.assertIsNone(findmy_status("1eff4c001219"))
