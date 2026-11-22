# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import hashlib
import struct
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from dashboard import ble, ble_stream, flash
from dashboard.hubble_ble import hubble_identities, match_hubble
from dashboard.phyphox import LightClient, local_phone_url


class BenchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.keys = self.root / "secrets/tag-keys"
        self.keys.mkdir(parents=True)
        self.entry = dict(tag="Hagalu-003", board="S6L", network="findmy", burned_at="2026-11-22",
                          evidence="Programmer output")

    def test_record_retry_and_private_download(self):
        config = self.root / "tool.cfg"
        config.write_bytes(b'{"advSet": []}')
        entry = flash.record(self.root, self.entry, config)
        self.assertEqual(entry, flash.record(self.root, self.entry, config))
        self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), entry["config_sha256"])
        name = "hagalu_003.programmed.cfg"
        self.assertEqual(config.read_bytes(), flash.download(self.root, name))
        with self.assertRaises(ValueError):
            flash.record(self.root, {**self.entry, "board": "N6"}, config)
        flash.archived_config(self.root, self.entry["tag"]).chmod(0o600)
        flash.archived_config(self.root, self.entry["tag"]).write_text("{}")
        self.assertIsNone(flash.download(self.root, name))
        self.assertIsNone(flash.download(self.root, "../../secrets/access-code.txt"))

        key = self.keys / "hagalu_003.keys"
        key.symlink_to(config)
        self.assertIsNone(flash.download(self.root, key.name))
        key.unlink()
        key.write_text("test-key")
        self.assertEqual(b"test-key", flash.download(self.root, key.name))
        self.assertNotIn("test-key", flash.page(self.root))

    def test_btsnoop_checks_payload_identity_and_both_hci_report_formats(self):
        identity = bytes(range(28))
        (self.keys / "hagalu_003.keys").write_bytes(b"Advertisement key: " + base64.b64encode(identity))
        address = (bytes([0xC0]) + identity[1:6])[::-1]
        data = b"\x1e\xff\x4c\x00\x12\x19\x2a" + identity[6:] + b"\x00\x00"
        header = b"\x00\x01" + address
        legacy = b"\x3e\x00\x02\x02" + header + bytes([len(data)]) + data + b"\xc2"
        legacy += header + bytes([len(data)]) + data[:-3] + b"\xa1\x00\x00\xc3"
        extended_header = struct.pack("<HB6sBBBbbHB6sB", 0, 1, address, 1, 0, 0, 0, -63, 0, 0,
                                      bytes(6), len(data))
        extended = b"\x3e\x00\x0d\x01" + extended_header + data
        capture = self.root / "radio.btsnoop"
        with capture.open("wb") as file:
            file.write(ble.SNOOP_HEADER)
            for seconds, packet in enumerate((legacy, extended), 1):
                file.write(struct.pack(">IIIIq", len(packet), len(packet), 3, 0,
                                       ble.SNOOP_EPOCH + seconds * 1_000_000))
                file.write(packet)
        tag = ble.analyze_capture(capture, self.keys)["tags"]["Hagalu-003"]
        self.assertEqual(2, tag["reports"])
        self.assertEqual(-63, tag["rssi"])
        self.assertEqual(42, tag["status_byte"])

    def test_stream_tag_alias_and_signed_temperature(self):
        self.assertEqual("Hagalu-003", ble_stream.resolve("h03"))
        self.assertEqual("Hagalu-004", ble_stream.resolve("hagalu_004"))
        self.assertEqual("Irulu-012", ble_stream.resolve("i12"))
        self.assertEqual("h03", ble_stream.short_name("Hagalu-003"))
        self.assertEqual(-1.5, ble_stream.temperature({"findmy_temp": True}, "findmy", b"\x12\x19\xfd"))
        self.assertIsNone(ble_stream.temperature({}, "findmy", b"\x12\x19\x00"))

    def test_received_interval_and_shared_time_buckets(self):
        self.assertIsNone(ble_stream.interval_between(None, 12))
        self.assertIsNone(ble_stream.interval_between(12, 11))
        self.assertEqual(3.125, ble_stream.interval_between(12, 15.125))
        self.assertEqual("·▁·█", ble_stream.graph([(2.5, 0), (10, 10)], 0, 10, 4, 0, 10))

    def test_phyphox_maps_samples_to_phone_wall_time_across_resume(self):
        client = LightClient("http://192.168.1.15:8080")
        client.armed_at = 1005
        anchors = [
            {"event": "START", "experimentTime": 0.0, "systemTime": 1000.0},
            {"event": "PAUSE", "experimentTime": 10.0, "systemTime": 1010.0},
            {"event": "START", "experimentTime": 10.0, "systemTime": 1020.0},
        ]
        data = {"status": {"measuring": True}, "buffer": {
            "illum_time": {"buffer": [9.5, 10.125]},
            "illum": {"buffer": [550, 1700]},
        }}
        with patch.object(client, "get", side_effect=[anchors, data, anchors, data]) as get:
            self.assertEqual(("Measuring", [(1009.5, 9.5, 550), (1020.125, 10.125, 1700)]), client.poll())
            self.assertEqual(("Measuring", []), client.poll())
            self.assertEqual({"illum_time": "10.125", "illum": "10.125|illum_time"}, get.call_args.args[1])
        with self.assertRaises(ValueError):
            local_phone_url("http://example.org:8080")

    def test_phyphox_keeps_the_last_reading_before_the_ble_run(self):
        client = LightClient("http://192.168.1.15:8080")
        client.armed_at = 1100
        anchors = [{"event": "START", "experimentTime": 0.0, "systemTime": 1000.0}]
        data = {"status": {"measuring": True}, "buffer": {
            "illum_time": {"buffer": [35, 40]},
            "illum": {"buffer": [500, 10558.6]},
        }}
        with patch.object(client, "get", side_effect=[anchors, data, anchors, data]):
            self.assertEqual(("Measuring", [(1040, 40, 10558.6)]), client.poll())
            self.assertEqual(("Measuring", []), client.poll())

    def test_hubble_requires_an_authenticated_matching_advertisement(self):
        (self.keys / "hagalu_002.hubble.key").write_text(
            "key_hex: 000102030405060708090a0b0c0d0e0f\nperiod_exponent: 15\n"
        )
        identities = hubble_identities(self.keys)
        advert = bytes.fromhex("08aa55f7a58a10e5dda9fdf0a857d81af51237")
        self.assertEqual("Hagalu-002", match_hubble(identities, advert))
        self.assertIsNone(match_hubble(identities, advert[:-1] + b"\x00"))

    def test_reception_counts_are_incremented_across_flushes(self):
        tag = "Hagalu-003"
        minute = "2026-11-22T16:09"
        reception = {"reports": 3, "rssi": -62, "status_byte": 42, "last_seen": minute + ":34+00:00"}
        ble.log_receptions(self.root, {(tag, minute): reception})
        ble.log_receptions(self.root, {(tag, minute): {**reception, "reports": 2}})
        self.assertEqual([(tag, minute, 5, reception["last_seen"], -62, 42)],
                         ble.reception_history(self.root, minute))
