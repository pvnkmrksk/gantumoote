# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantumoote, https://github.com/pvnkmrksk/gantumoote (see NOTICE).
"""Synthetic data only: isolation, secrets, key validation, cursors and provider failures."""

import base64
import json
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch
from .portal import Portal, ProviderError, hubble_sensors, status_temperature, valid_key


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.portal = Portal(self.root)
        self.primary = self.portal.add({"name": "Main", "private_key": self.key(1)})["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def key(self, n):
        return base64.b64encode(n.to_bytes(28, "big")).decode()

    def test_keys_reject_invalid_and_duplicate(self):
        for value in ["bad", self.key(0), self.key(2**224 - 1)]:
            with self.assertRaises(ValueError):
                valid_key(value)
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.portal.add({"name": "Duplicate", "private_key": self.key(1)})

    def test_dynamic_add_is_private_and_isolated(self):
        ident = self.portal.add({"name": "Second", "private_key": self.key(2)})["id"]
        d = self.portal.device(ident)
        self.portal.save_reports(
            d, [{"timestamp": "2026-01-01T00:00:00Z", "latitude": 1, "longitude": 2, "accuracy_m": 3}]
        )
        self.assertEqual(self.portal.history(ident)["report_count"], 1)
        self.assertEqual(self.portal.history()["report_count"], 0)
        data = json.dumps(self.portal.inventory())
        self.assertNotIn(self.key(2), data)
        self.assertNotIn("key_file", data)
        self.portal.toggle({"id": ident, "active": False})
        self.assertEqual(self.portal.history(ident)["report_count"], 1)
        self.assertEqual((self.root / json.loads(d["config"])["key_file"]).stat().st_mode & 0o777, 0o600)

    def test_rename_keeps_history_and_leaves_hubble_names_to_hubble(self):
        d = self.portal.device(self.primary)
        report = {"timestamp": "2026-01-01T00:00:00Z", "latitude": 1, "longitude": 2, "accuracy_m": 3}
        self.portal.save_reports(d, [report])
        self.portal.rename({"id": self.primary, "name": "  Hagalu-001 "})
        history = self.portal.history(self.primary)
        self.assertEqual((history["tag"], history["report_count"]), ("Hagalu-001", 1))
        for name in ["", " ", "x" * 101]:
            with self.assertRaisesRegex(ValueError, "device name"):
                self.portal.rename({"id": self.primary, "name": name})
        with self.assertRaisesRegex(ValueError, "not found"):
            self.portal.rename({"id": "missing", "name": "Other"})
        self.prepare_hubble()
        hubble = self.portal.devices()[1]
        with self.assertRaisesRegex(ValueError, "Hubble dashboard"):
            self.portal.rename({"id": hubble["id"], "name": "Local name"})
        self.assertEqual(self.portal.device(self.primary)["name"], "Hagalu-001")

    def test_apple_devices_share_one_cycle_and_keep_reports_separate(self):
        from types import SimpleNamespace

        ident = self.portal.add({"name": "Second", "private_key": self.key(2)})["id"]
        report = {"timestamp": "2026-01-01T00:00:00Z", "latitude": 1, "longitude": 2, "accuracy_m": 3}
        other = {**report, "latitude": 4}
        result = SimpleNamespace(
            stdout=json.dumps({"ok": True, "devices": {self.primary: [report], ident: [other]}})
        )
        with patch("dashboard.portal.subprocess.run", return_value=result) as worker:
            self.portal.apple(threading.Event(), lambda *args: None)
            self.portal.apple(threading.Event(), lambda *args: None)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(len(json.loads(worker.call_args.kwargs["input"])), 2)
        self.assertEqual(self.portal.history()["reports"][0]["latitude"], 1)
        self.assertEqual(self.portal.history(ident)["reports"][0]["latitude"], 4)

    def connect(self):
        self.portal.connect({"org_id": "org-test", "token": "synthetic-token"})
        with self.portal.db() as c:
            return dict(c.execute("SELECT * FROM connections").fetchone())

    def test_hubble_discovery_pagination_and_secret_redaction(self):
        con = self.connect()
        calls = []

        def api(con, path, cursor=None, query=None):
            calls.append(cursor)
            return (
                {"devices": [{"id": "one" if not cursor else "two", "name": "Device"}]},
                "page2" if not cursor else None,
                0,
            )

        with patch.object(self.portal, "hubble_request", side_effect=api):
            self.portal.discover(con)
        self.assertEqual(calls, [None, "page2"])
        self.assertEqual(len(self.portal.devices()), 3)
        self.assertNotIn("synthetic-token", json.dumps(self.portal.inventory()))

    def test_hubble_temperature_unit_is_per_device_and_survives_discovery(self):
        con = self.prepare_hubble()
        packet = self.packet()
        # VCC 0x6d, temperature 42 counts, advert count 5
        packet["device"]["payload"] = base64.b64encode(bytes.fromhex("6d2a0005000000")).decode()
        self.portal.ingest_packets(con, [packet])
        hub = self.portal.devices()[1]["id"]
        self.assertEqual(self.portal.packets(hub)["packets"][0]["sensors"]["temp_c"], 0.42)  # 0.01 degC default
        history = self.portal.history(hub)
        self.assertEqual((history["temp_unit"], history["reports"][0]["temp_c"]), (0.01, 0.42))

        self.portal.set_temp_unit({"id": hub, "temp_unit": 0.5})
        self.assertEqual(self.portal.packets(hub)["packets"][0]["sensors"]["temp_c"], 21.0)
        self.assertEqual(self.portal.history(hub)["reports"][0]["temp_c"], 21.0)  # the sighting gets its packet's reading
        with patch.object(
            self.portal, "hubble_request", return_value=({"devices": [{"id": "one", "name": "Renamed"}]}, None, 0)
        ):
            self.portal.discover(con)
        self.assertEqual(self.portal.history(hub)["temp_unit"], 0.5)  # a sync keeps the setting
        self.assertEqual(self.portal.device(hub)["name"], "Renamed")
        self.assertEqual(next(d for d in self.portal.inventory()["devices"] if d["id"] == hub)["temp_unit"], 0.5)

        for unit in [0.1, "0.5", True, None]:
            with self.assertRaisesRegex(ValueError, "0.01 or 0.5"):
                self.portal.set_temp_unit({"id": hub, "temp_unit": unit})
        with self.assertRaisesRegex(ValueError, "Only Hubble"):
            self.portal.set_temp_unit({"id": self.primary, "temp_unit": 0.5})
        self.assertIsNone(self.portal.history(self.primary)["temp_unit"])

    def test_status_byte_is_kept_and_read_as_temperature_only_when_set(self):
        d = self.portal.device(self.primary)
        base = {"latitude": 1, "longitude": 2, "accuracy_m": 3}
        old = {"timestamp": "2026-01-01T00:00:00Z", **base}  # saved before status bytes were kept
        self.portal.save_reports(d, [old])
        added = self.portal.save_reports(
            d,
            [
                {**old, "status": 0x2C},  # the same sighting again, now with its status byte
                {"timestamp": "2026-01-01T00:01:00Z", **base, "status": 0xFE},
                {"timestamp": "2026-01-01T00:02:00Z", **base, "status": True},  # not a byte
            ],
        )
        self.assertEqual(added, 2)
        reports = self.portal.history(self.primary)["reports"]
        self.assertEqual([r.get("status") for r in reports], [0x2C, 0xFE, None])
        self.assertFalse(any("temp_c" in r for r in reports))  # a plain tag's byte is not a temperature

        self.portal.set_status_format({"id": self.primary, "status_format": "temp_half_c"})
        history = self.portal.history(self.primary)
        self.assertEqual(history["status_format"], "temp_half_c")
        self.assertEqual([r.get("temp_c") for r in history["reports"]], [22.0, -1.0, None])
        self.assertEqual(self.portal.inventory()["devices"][0]["status_format"], "temp_half_c")

        self.portal.set_status_format({"id": self.primary, "status_format": None})
        self.assertFalse(any("temp_c" in r for r in self.portal.history(self.primary)["reports"]))
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.portal.set_status_format({"id": self.primary, "status_format": "volts"})
        self.prepare_hubble()
        with self.assertRaisesRegex(ValueError, "Only OpenHaystack"):
            self.portal.set_status_format({"id": self.portal.devices()[1]["id"], "status_format": "temp_half_c"})

    def test_status_temperature_decoding(self):
        cases = [(0x00, 0.0), (0x26, 19.0), (0x33, 25.5), (0x50, 40.0), (0x7F, 63.5), (0x80, -64.0), (0xFF, -0.5)]
        for status, temp in cases:
            self.assertEqual(status_temperature("temp_half_c", status), temp)
        for status in [None, -1, 256, "0x26", True]:
            self.assertIsNone(status_temperature("temp_half_c", status))
        self.assertIsNone(status_temperature(None, 0x26))

    def prepare_hubble(self):
        con = self.connect()
        with patch.object(
            self.portal, "hubble_request", return_value=({"devices": [{"id": "one", "name": "Tag"}]}, None, 0)
        ):
            self.portal.discover(con)
        with self.portal.db() as c:
            c.execute("UPDATE connections SET next_discovery=9999999999")
        return con

    def packet(self):
        return {
            "device": {"id": "one", "timestamp": 1700000000, "payload": "AQI="},
            "location": {"latitude": 1, "longitude": 2, "horizontal_accuracy": 4, "timestamp": 1700000000},
        }

    def test_packet_dedup_and_no_invented_locations(self):
        con = self.prepare_hubble()
        packet = self.packet()
        self.portal.ingest_packets(con, [packet, packet])
        d = self.portal.devices()[1]
        self.assertEqual(self.portal.history(d["id"])["report_count"], 1)
        self.assertEqual(len(self.portal.packets(d["id"])["packets"]), 1)
        p = {"device": {"id": "one", "timestamp": 1700000001, "payload": "AwQ="}}
        self.portal.ingest_packets(con, [p])
        self.assertEqual(self.portal.history(d["id"])["report_count"], 1)
        self.assertEqual(len(self.portal.packets(d["id"])["packets"]), 2)

    def test_sensor_payload_in_archived_packets_without_location(self):
        con = self.prepare_hubble()
        encoded = base64.b64encode(bytes.fromhex("6d cc07 0f000000")).decode()
        packet = {"device": {"id": "one", "timestamp": 1700000001, "payload": encoded}}
        self.portal.ingest_packets(con, [self.packet(), packet])
        ident = self.portal.devices()[1]["id"]
        newest, older = self.portal.packets(ident)["packets"]
        self.assertEqual(newest["sensors"], {"vcc_v": 3.406, "temp_c": 19.96, "adv_count": 15})
        self.assertNotIn("sensors", older)
        self.assertEqual(self.portal.history(ident)["report_count"], 1)
        raw = base64.b64encode(bytes.fromhex("00 85ff ffffffff")).decode()
        self.assertEqual(
            hubble_sensors({"device": {"payload": raw}}),
            {"vcc_v": 0, "temp_c": -1.23, "adv_count": 4294967295},
        )
        for value in (None, 5, "!!!xxxxxxxxx", "AAAAAAAAAAAA", "AA=="):
            self.assertIsNone(hubble_sensors({"device": {"payload": value}}))

    def test_keys_and_device_deletions_survive_restart(self):
        con = self.prepare_hubble()
        hubble = self.portal.devices()[1]["id"]
        apple = self.portal.add({"name": "Second", "private_key": self.key(2)})["id"]
        secret = bytes(range(16)).hex()
        with self.assertRaises(ValueError):
            self.portal.save_hubble_key({"id": hubble, "private_key": "too short"})
        self.portal.save_hubble_key({"id": hubble, "private_key": secret})
        self.portal = Portal(self.root)
        saved = self.portal.hubble_key_path(hubble)
        self.assertEqual(base64.b64decode(saved.read_bytes()), bytes.fromhex(secret))
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertTrue(next(d for d in self.portal.inventory()["devices"] if d["id"] == hubble)["key_saved"])
        self.assertNotIn(saved.read_text(), json.dumps(self.portal.inventory()))
        self.assertEqual(self.portal.device(apple)["name"], "Second")
        self.portal.delete({"id": self.primary})
        self.assertEqual(self.portal.history()["device_id"], hubble)
        self.portal.delete({"id": apple})
        self.portal.delete({"id": hubble})
        self.portal = Portal(self.root)
        self.assertEqual(self.portal.inventory()["devices"], [])
        self.assertIsNone(self.portal.history()["device_id"])
        self.assertEqual(self.portal.packets(None), {"packets": []})
        with self.assertRaisesRegex(ValueError, "Device not found"):
            self.portal.packets(hubble)
        self.assertEqual(self.portal.ingest_packets(con, [self.packet()]), 0)
        self.assertTrue(saved.is_file())

    def test_deleted_hubble_aliases_stay_deleted_after_discovery_and_packets(self):
        con = self.prepare_hubble()
        tag = self.portal.devices()[1]
        with self.portal.db() as c:
            c.execute(
                "INSERT INTO devices(id,name,provider,active,config) VALUES(?,?,?,?,?)",
                ("older-alias", "Old name", "hubble", 1, tag["config"]),
            )
        self.portal.delete({"id": "older-alias"})
        body = {"devices": [{"id": "one", "name": "Tag renamed"}, {"id": "two", "name": "Second tag"}]}
        with patch.object(self.portal, "hubble_request", return_value=(body, None, 0)):
            self.portal.discover(con)
        self.assertEqual([d["name"] for d in self.portal.devices()], ["Main", "Second tag"])
        second = {"device": {"id": "two", "timestamp": 1700000001, "payload": "AA=="}}
        self.assertEqual(self.portal.ingest_packets(con, [self.packet(), second]), 1)

    def test_apple_poll_does_not_save_reports_for_device_deleted_mid_fetch(self):
        ident = self.portal.add({"name": "Second", "private_key": self.key(2)})["id"]
        path = self.portal.path_for(self.portal.device(ident))

        def fetch(*args, **kwargs):
            from types import SimpleNamespace

            self.portal.delete({"id": ident})
            report = {"timestamp": "2026-01-01T00:00:00Z", "latitude": 1, "longitude": 2, "accuracy_m": 3}
            return SimpleNamespace(stdout=json.dumps({"ok": True, "devices": {ident: [report]}}))

        with patch("dashboard.portal.subprocess.run", side_effect=fetch):
            self.portal.apple(threading.Event(), lambda *args: self.fail("Unexpected notification"))
        self.assertFalse(path.exists())

    def test_cursor_commits_after_durable_ingest_and_respects_retry(self):
        self.prepare_hubble()
        with patch.object(
            self.portal, "hubble_request", return_value=({"packets": [self.packet()]}, "next-page", 120)
        ) as api:
            self.portal.hubble(lambda *a: None)
        self.assertEqual(api.call_count, 1)
        with self.portal.db() as c:
            con = dict(c.execute("SELECT * FROM connections").fetchone())
        self.assertEqual(con["cursor"], "next-page")
        self.assertEqual(con["state"], "ok")

    def test_failed_ingest_does_not_advance_cursor(self):
        self.prepare_hubble()
        with (
            patch.object(
                self.portal, "hubble_request", return_value=({"packets": [self.packet()]}, "unsafe", 0)
            ),
            patch.object(self.portal, "ingest_packets", side_effect=OSError()),
        ):
            self.portal.hubble(lambda *a: None)
        with self.portal.db() as c:
            con = dict(c.execute("SELECT * FROM connections").fetchone())
        self.assertIsNone(con["cursor"])
        self.assertEqual(con["state"], "backoff")

    def test_new_device_is_discovered_from_its_first_packet(self):
        self.prepare_hubble()
        packet = self.packet()
        packet["device"]["id"] = "two"

        def api(con, path, cursor=None, query=None):
            if path.endswith("/devices"):
                return {"devices": [{"id": "one", "name": "Tag"}, {"id": "two", "name": "New tag"}]}, None, 0
            return {"packets": [packet]}, None, 0

        with patch.object(self.portal, "hubble_request", side_effect=api):
            self.portal.hubble(lambda *a: None)
        new = [d for d in self.portal.devices() if d["name"] == "New tag"]
        self.assertEqual(len(new), 1)
        self.assertEqual(len(self.portal.packets(new[0]["id"])["packets"]), 1)

    def test_stale_or_malformed_packets_do_not_block_the_stream(self):
        self.prepare_hubble()
        removed = {"device": {"id": "removed", "timestamp": 1700000000, "payload": "AA=="}}
        no_time = {"device": {"id": "one", "payload": "AA=="}}
        bad_location = self.packet()
        bad_location["device"]["timestamp"] = 1700000002
        bad_location["location"]["latitude"] = "north"

        def api(con, path, cursor=None, query=None):
            if path.endswith("/devices"):
                return {"devices": [{"id": "one", "name": "Tag"}]}, None, 0
            if cursor:
                return {"packets": []}, "next", 0
            return {"packets": [removed, no_time, bad_location, self.packet()]}, "next", 0

        with self.assertLogs(level="WARNING") as logs, patch.object(
            self.portal, "hubble_request", side_effect=api
        ):
            self.portal.hubble(lambda *a: None)
        self.assertEqual(len(logs.records), 3)
        with self.portal.db() as c:
            con = dict(c.execute("SELECT * FROM connections").fetchone())
        self.assertEqual((con["cursor"], con["state"]), ("next", "ok"))
        tag = self.portal.devices()[1]["id"]
        self.assertEqual(len(self.portal.packets(tag)["packets"]), 2)
        self.assertEqual(self.portal.history(tag)["report_count"], 1)

    def test_auth_failure_is_separate_from_apple(self):
        self.prepare_hubble()
        with patch.object(self.portal, "hubble_request", side_effect=ProviderError("HubbleHTTP401", 401)):
            self.portal.hubble(lambda *a: None)
        with self.portal.db() as c:
            con = dict(c.execute("SELECT * FROM connections").fetchone())
        self.assertEqual(con["state"], "needs_connection")
        self.assertFalse(self.portal.apple_login)

    def test_rate_limit_backoff(self):
        self.prepare_hubble()
        with patch.object(
            self.portal, "hubble_request", side_effect=ProviderError("HubbleHTTP429", 429, "1800")
        ):
            self.portal.hubble(lambda *a: None)
        with self.portal.db() as c:
            con = dict(c.execute("SELECT * FROM connections").fetchone())
        import time

        self.assertGreater(con["next_poll"] - time.time(), 1700)


if __name__ == "__main__":
    unittest.main()
