"""Device registry and serialized provider collection; never return credentials."""

import base64
import hashlib
import json
import logging
import math
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from cryptography.hazmat.primitives.asymmetric import ec

from tools.hubble_decode import parse_sensors
from .settings import PROJECT, REGISTRY, write_private


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def location_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=15, factory=ClosingConnection)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS sightings(timestamp TEXT,latitude REAL,longitude REAL,accuracy_m REAL,first_fetched_at TEXT,UNIQUE(timestamp,latitude,longitude,accuracy_m))"
    )
    c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)")
    return c


def valid_key(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 28:
            raise ValueError()
        ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP224R1())
        # cryptography may reduce some scalars modulo the order; reject non-canonical keys.
        if int.from_bytes(raw, "big") >= 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFF16A2E0B8F03E13DD29455C5C2A3D:
            raise ValueError()
        return raw
    except Exception:
        raise ValueError("Use a valid 28-byte OpenHaystack private key in standard base64") from None


def hubble_sensors(packet):
    """Read the decrypted seven-byte Gantu payload returned by Hubble."""
    device = packet.get("device")
    encoded = device.get("payload") if isinstance(device, dict) else None
    if not isinstance(encoded, str) or len(encoded) != 12:
        return None
    try:
        return parse_sensors(base64.b64decode(encoded, validate=True))
    except ValueError:
        return None


class Portal:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / REGISTRY
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT, provider TEXT, active INTEGER, config TEXT,
                    state TEXT DEFAULT 'waiting', error TEXT, checked_at TEXT,
                    next_poll REAL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, org_id TEXT UNIQUE, secret_file TEXT, cursor TEXT,
                    next_poll REAL DEFAULT 0, next_discovery REAL DEFAULT 0,
                    state TEXT DEFAULT 'waiting', error TEXT, failures INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS packets (
                    device_id TEXT, fingerprint TEXT, observed_at TEXT, payload TEXT,
                    PRIMARY KEY(device_id, fingerprint)
                );
            """)
            if "deleted" not in {row["name"] for row in c.execute("PRAGMA table_info(devices)")}:
                c.execute("ALTER TABLE devices ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        self.apple_next = 0
        self.apple_failures = 0
        self.apple_login = False

    def db(self):
        c = sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def devices(self):
        with self.db() as c:
            rows = c.execute("SELECT * FROM devices WHERE deleted=0 ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def device(self, ident):
        with self.db() as c:
            r = c.execute("SELECT * FROM devices WHERE id=? AND deleted=0", (ident,)).fetchone()
        if not r:
            raise ValueError("Device not found")
        return dict(r)

    @property
    def default(self):
        with self.db() as c:
            row = c.execute("SELECT id FROM devices WHERE deleted=0 ORDER BY rowid LIMIT 1").fetchone()
        return row["id"] if row else None

    def path_for(self, d):
        return self.root / json.loads(d["config"])["database"]

    def history(self, ident=None):
        with self.lock:
            ident = ident or self.default
            if ident:
                return self.device_history(self.device(ident))
        return {
            "device_id": None,
            "tag": "Gantu",
            "provider": None,
            "reports": [],
            "report_count": 0,
            "checked_at": None,
            "latest_observation": None,
            "collector": {"state": "idle", "error": None, "next_poll_at": None},
        }

    def device_history(self, d):
        reports = []
        cfg = json.loads(d["config"])
        if cfg.get("database"):
            with location_db(self.path_for(d)) as c:
                reports = [
                    dict(zip(["timestamp", "latitude", "longitude", "accuracy_m"], r))
                    for r in c.execute(
                        "SELECT timestamp,latitude,longitude,accuracy_m FROM sightings ORDER BY timestamp,latitude,longitude,accuracy_m"
                    )
                ]
                row = c.execute("SELECT value FROM meta WHERE key='checked_at'").fetchone()
                checked = row[0] if row else d["checked_at"]
        else:
            checked = d["checked_at"]
        return {
            "device_id": d["id"],
            "tag": d["name"],
            "provider": d["provider"],
            "reports": reports,
            "report_count": len(reports),
            "checked_at": checked,
            "latest_observation": reports[-1]["timestamp"] if reports else None,
            "collector": {
                "state": d["state"],
                "error": d["error"],
                "next_poll_at": datetime.fromtimestamp(d["next_poll"], timezone.utc).isoformat()
                if d["next_poll"]
                else None,
            },
        }

    def inventory(self):
        items = []
        for d in self.devices():
            h = self.device_history(d)
            with self.db() as c:
                n = c.execute("SELECT count(*) FROM packets WHERE device_id=?", (d["id"],)).fetchone()[0]
            items.append(
                {k: d[k] for k in ("id", "name", "provider", "active", "state", "error")}
                | {
                    "report_count": h["report_count"],
                    "packet_count": n,
                    "checked_at": h["checked_at"],
                    "latest_observation": h["latest_observation"],
                    "key_saved": d["provider"] == "hubble" and self.hubble_key_path(d["id"]).is_file(),
                }
            )
        with self.db() as c:
            connections = [dict(r) for r in c.execute("SELECT id,org_id,state,error FROM connections")]
        default = items[0]["id"] if items else None
        return {"devices": items, "connections": connections, "default_device": default}

    def add(self, data):
        name = str(data.get("name", "")).strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("Enter a device name (1–100 characters)")
        raw = valid_key(str(data.get("private_key", "")).strip())
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            for d in self.devices():
                if d["provider"] != "openhaystack":
                    continue
                cfg = json.loads(d["config"])
                p = self.root / cfg["key_file"]
                content = p.read_bytes()
                existing = base64.b64decode(content.strip()) if cfg.get("key_format") == "base64" else content
                if hashlib.sha256(existing).hexdigest() == digest:
                    raise ValueError("This OpenHaystack key is already registered")
            ident = uuid.uuid4().hex
            keyfile = f"secrets/devices/{ident}.b64"
            p = self.root / keyfile
            write_private(p, base64.b64encode(raw))
            cfg = {"key_file": keyfile, "key_format": "base64", "database": f"devices/{ident}.sqlite3"}
            try:
                with self.db() as c:
                    c.execute(
                        "INSERT INTO devices(id,name,provider,active,config) VALUES(?,?,?,?,?)",
                        (ident, name, "openhaystack", 1, json.dumps(cfg)),
                    )
            except sqlite3.Error:
                p.unlink(missing_ok=True)
                raise
            # Join the next existing Apple cycle; adding never starts an extra fetch.
            return {"id": ident}

    def toggle(self, data):
        enabled = data.get("active")
        if not isinstance(enabled, bool):
            raise ValueError("active must be boolean")
        with self.lock:
            d = self.device(str(data.get("id", "")))
            if d["provider"] != "openhaystack":
                raise ValueError(
                    "Hubble is collected by organization. Delete the device to stop collecting it."
                )
            with self.db() as c:
                c.execute(
                    "UPDATE devices SET active=?,state=?,error=NULL WHERE id=?",
                    (enabled, "waiting" if enabled else "archived", d["id"]),
                )
        return {"ok": True}

    def delete(self, data):
        """Remove a device from the portal and stop collection, retaining its private files."""
        ident = str(data.get("id", ""))
        with self.lock, self.db() as c:
            device = c.execute("SELECT * FROM devices WHERE id=?", (ident,)).fetchone()
            if not device:
                raise ValueError("Device not found")
            c.execute("UPDATE devices SET deleted=1, active=0 WHERE id=?", (ident,))
            config = json.loads(device["config"])
            if device["provider"] == "hubble" and config.get("remote_id"):
                # Older registries may contain two entries bound to the same remote device.
                c.execute(
                    """UPDATE devices SET deleted=1, active=0 WHERE provider='hubble'
                             AND json_extract(config, '$.connection')=?
                             AND json_extract(config, '$.remote_id')=?""",
                    (config["connection"], config["remote_id"]),
                )
        return {"ok": True}

    def hubble_key_path(self, ident):
        return self.root / "secrets/devices" / f"{ident}.hubble.b64"

    def save_hubble_key(self, data):
        encoded = str(data.get("private_key", "")).strip()
        try:
            raw = (
                bytes.fromhex(encoded)
                if re.fullmatch(r"[a-fA-F0-9]{32}", encoded)
                else base64.b64decode(encoded, validate=True)
            )
        except ValueError:
            raw = b""
        if len(raw) != 16:
            raise ValueError("Enter a 16-byte Hubble key in hex or standard base64")
        with self.lock:
            device = self.device(str(data.get("id", "")))
            if device["provider"] != "hubble":
                raise ValueError("Select a Hubble device")
            write_private(self.hubble_key_path(device["id"]), base64.b64encode(raw))
        return {"ok": True}

    def connect(self, data):
        org = str(data.get("org_id", "")).strip()
        token = str(data.get("token", "")).strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", org)
            or not token
            or len(token) > 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("Enter a valid organization ID and API token")
        ident = hashlib.sha256(org.encode()).hexdigest()[:24]
        rel = f"secrets/hubble/{ident}.json"
        p = self.root / rel
        with self.lock:
            write_private(p, json.dumps({"token": token}).encode())
            with self.db() as c:
                c.execute(
                    "INSERT INTO connections(id,org_id,secret_file) VALUES(?,?,?) ON CONFLICT(org_id) DO UPDATE SET next_poll=0,next_discovery=0,state='waiting',error=NULL,failures=0",
                    (ident, org, rel),
                )
        return {"ok": True, "message": "Connection saved; discovery runs in the background"}

    def packets(self, ident):
        with self.lock:
            ident = ident or self.default
            if not ident:
                return {"packets": []}
            self.device(ident)
            with self.db() as c:
                rows = c.execute(
                    "SELECT observed_at,payload FROM packets WHERE device_id=? ORDER BY observed_at DESC LIMIT 50",
                    (ident,),
                ).fetchall()
        packets = []
        for row in rows:
            packet = json.loads(row["payload"])
            entry = {"timestamp": row["observed_at"], "packet": packet}
            if sensors := hubble_sensors(packet):
                entry["sensors"] = sensors
            packets.append(entry)
        return {"packets": packets}

    def status(self, ids, state, error=None, delay=0):
        with self.db() as c:
            for ident in ids:
                c.execute(
                    "UPDATE devices SET state=?,error=?,next_poll=? WHERE id=? AND active=1 AND deleted=0",
                    (state, error, time.time() + delay if delay else 0, ident),
                )

    def save_reports(self, d, reports):
        added = 0
        with location_db(self.path_for(d)) as c:
            for r in reports:
                lat = float(r["latitude"])
                lon = float(r["longitude"])
                acc = float(r["accuracy_m"])
                if not all(math.isfinite(v) for v in (lat, lon, acc)) or not (
                    -90 <= lat <= 90 and -180 <= lon <= 180 and acc >= 0
                ):
                    continue
                result = c.execute(
                    "INSERT OR IGNORE INTO sightings VALUES(?,?,?,?,?)",
                    (timestamp(r["timestamp"]), lat, lon, acc, now()),
                )
                added += result.rowcount
            c.execute("INSERT OR REPLACE INTO meta VALUES('checked_at',?)", (now(),))
        with self.db() as c:
            c.execute("UPDATE devices SET checked_at=? WHERE id=?", (now(), d["id"]))
        return added

    def apple(self, stop, push):
        ds = [d for d in self.devices() if d["provider"] == "openhaystack" and d["active"]]
        if not ds or time.time() < self.apple_next:
            return
        if self.apple_login:
            newly_added = [d["id"] for d in ds if d["state"] != "needs_login"]
            if newly_added:
                self.status(newly_added, "needs_login", "AppleLoginRequired")
            return
        ids = [d["id"] for d in ds]
        self.status(ids, "polling")
        start = time.monotonic()
        try:
            items = [{"id": d["id"], **json.loads(d["config"])} for d in ds]
            result = subprocess.run(
                [sys.executable, "-m", "dashboard.apple", "fetch"],
                cwd=PROJECT,
                input=json.dumps(items),
                capture_output=True,
                text=True,
                timeout=100,
            )
            data = json.loads(result.stdout)
            if not data.get("ok"):
                if data.get("http_status") in (401, 403) or data.get("error") in (
                    "AppleLoginRequired",
                    "UnauthorizedError",
                    "InvalidCredentialsError",
                    "InvalidStateError",
                ):
                    self.apple_login = True
                    self.status(ids, "needs_login", data.get("error"))
                    return
                raise ProviderError(
                    data.get("error", "FetchError"), data.get("http_status"), data.get("retry_after")
                )
            for d in ds:
                with self.lock:
                    try:
                        if not self.device(d["id"])["active"]:
                            continue
                    except ValueError:
                        continue
                    added = self.save_reports(d, data["devices"].get(d["id"], []))
                if added:
                    push(
                        d["name"] + ": new location report", f"{added} new sighting(s). Open the map to view."
                    )
            delay = max(1, 60 - (time.monotonic() - start))
            self.apple_failures = 0
            self.status(ids, "ok", delay=delay)
        except Exception as e:
            self.apple_failures += 1
            delay = min(3600, 300 * 2 ** min(self.apple_failures - 1, 4))
            if isinstance(e, ProviderError) and e.code == 429:
                delay = max(delay, 900, e.retry)
            self.status(ids, "backoff", e.name if isinstance(e, ProviderError) else type(e).__name__, delay)
        self.apple_next = time.time() + delay

    def hubble_request(self, con, path, cursor=None, query=None):
        token = json.loads((self.root / con["secret_file"]).read_text())["token"]
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "GantuDashboard/1.0",
        }
        if cursor:
            headers["Continuation-Token"] = cursor
        url = "https://api.hubble.com" + path
        if query and not cursor:
            url += "?" + urlencode(query)
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                body = response.read(20 * 1024 * 1024 + 1)
                if len(body) > 20 * 1024 * 1024:
                    raise ValueError("Response too large")
                return (
                    json.loads(body),
                    response.headers.get("Continuation-Token"),
                    int(response.headers.get("Retry-After") or 0),
                )
        except HTTPError as e:
            raise ProviderError("HubbleHTTP" + str(e.code), e.code, e.headers.get("Retry-After")) from None

    def discover(self, con):
        cursor = None
        for page in range(100):
            body, cursor, _ = self.hubble_request(
                con, "/v2/org/" + quote(con["org_id"], safe="") + "/devices", cursor, {"limit": 100}
            )
            for item in body.get("devices", []):
                remote = str(item.get("device_id") or item.get("id") or "")
                if not remote or len(remote) > 200:
                    continue
                name = str(item.get("name") or remote)[:100]
                ident = "h-" + hashlib.sha256((con["id"] + remote).encode()).hexdigest()[:24]
                with self.lock, self.db() as c:
                    bound = c.execute(
                        """SELECT id FROM devices WHERE provider='hubble'
                                         AND json_extract(config, '$.connection')=?
                                         AND json_extract(config, '$.remote_id')=?
                                         ORDER BY deleted DESC, id=? DESC, rowid LIMIT 1""",
                        (con["id"], remote, ident),
                    ).fetchone()
                    if bound:
                        ident = bound["id"]
                    cfg = {
                        "connection": con["id"],
                        "remote_id": remote,
                        "database": f"devices/{ident}.sqlite3",
                    }
                    c.execute(
                        "INSERT INTO devices(id,name,provider,active,config) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,name=excluded.name",
                        (ident, name, "hubble", 1, json.dumps(cfg)),
                    )
            if not cursor:
                return
        raise ValueError("Device discovery exceeded page limit")

    def ingest_packets(self, con, packets, skip_unknown=False):
        with self.lock:
            return self.store_hubble_packets(con, packets, skip_unknown)

    def store_hubble_packets(self, con, packets, skip_unknown=False):
        with self.db() as c:
            devices = c.execute(
                """SELECT * FROM devices WHERE provider='hubble'
                                   AND json_extract(config, '$.connection')=? ORDER BY deleted, rowid""",
                (con["id"],),
            ).fetchall()
        mapping = {json.loads(d["config"]).get("remote_id"): d for d in devices}
        added = 0
        for packet in packets:
            device = packet.get("device") or {}
            remote = device.get("id") or device.get("device_id")
            d = mapping.get(remote)
            if not d:
                if skip_unknown:
                    # Still not listed after a fresh discovery (e.g. removed from the organization):
                    # skip it, so one stale packet can't hold up every other device's stream.
                    logging.warning("Skipped a Hubble packet from a device the organization doesn't list.")
                    continue
                raise UndiscoveredDevice("Undiscovered Hubble device; retry after discovery")
            if d["deleted"]:
                continue
            # Paused devices are still archived from the organization stream so the global cursor cannot lose their data.
            try:
                ts = timestamp(device.get("timestamp") or (packet.get("location") or {}).get("timestamp"))
            except (AttributeError, TypeError, ValueError, OverflowError, OSError):
                logging.warning("Skipped a Hubble packet without a usable timestamp.")
                continue
            encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            with self.db() as c:
                added += c.execute(
                    "INSERT OR IGNORE INTO packets VALUES(?,?,?,?)", (d["id"], fingerprint, ts, encoded)
                ).rowcount
            loc = packet.get("location") or {}
            if (
                loc.get("timestamp")
                and loc.get("horizontal_accuracy") is not None
                and loc.get("latitude") is not None
                and loc.get("longitude") is not None
            ):
                # Terrestrial coordinates are the relay gateway's GPS fix, not on-board GPS.
                try:
                    self.save_reports(
                        d,
                        [
                            {
                                "timestamp": ts,
                                "latitude": loc["latitude"],
                                "longitude": loc["longitude"],
                                "accuracy_m": loc["horizontal_accuracy"],
                            }
                        ],
                    )
                except (TypeError, ValueError):
                    # The packet itself is archived above; only an unreadable location is left out.
                    logging.warning("Skipped an unreadable Hubble relay location.")
        return added

    def hubble(self, push):
        with self.db() as c:
            cons = [
                dict(r) for r in c.execute("SELECT * FROM connections WHERE next_poll<=?", (time.time(),))
            ]
        for con in cons:
            delay = 60
            state = "ok"
            error = None
            failures = 0
            try:
                if time.time() >= con["next_discovery"]:
                    self.discover(con)
                    with self.db() as c:
                        c.execute(
                            "UPDATE connections SET next_discovery=? WHERE id=?",
                            (time.time() + 300, con["id"]),
                        )
                cursor = con["cursor"]
                for page in range(5):
                    body, next_cursor, retry = self.hubble_request(
                        con,
                        "/v1/org/" + quote(con["org_id"], safe="") + "/packets",
                        cursor,
                        {"start": int(time.time()) - 7 * 86400},
                    )
                    packets = body.get("packets", [])
                    try:
                        added = self.ingest_packets(con, packets)
                    except UndiscoveredDevice:
                        # Most likely registered since the last discovery: look again now, then skip
                        # packets from any device the organization still doesn't list.
                        self.discover(con)
                        with self.db() as c:
                            c.execute(
                                "UPDATE connections SET next_discovery=? WHERE id=?",
                                (time.time() + 300, con["id"]),
                            )
                        added = self.ingest_packets(con, packets, skip_unknown=True)
                    # Advance only after durable idempotent writes to every device database.
                    with self.db() as c:
                        c.execute("UPDATE connections SET cursor=? WHERE id=?", (next_cursor, con["id"]))
                    if added:
                        push(
                            "Hubble: packets received",
                            f"{added} new packet(s) saved. Open the portal to view.",
                        )
                    if not packets or retry or not next_cursor or next_cursor == cursor:
                        delay = max(60, retry)
                        break
                    cursor = next_cursor
                    delay = 5
                with self.db() as c:
                    for d in self.devices():
                        if json.loads(d["config"]).get("connection") == con["id"] and d["active"]:
                            c.execute(
                                "UPDATE devices SET state='ok',checked_at=?,error=NULL,next_poll=? WHERE id=?",
                                (now(), time.time() + delay, d["id"]),
                            )
            except Exception as e:
                failures = con["failures"] + 1
                delay = min(3600, 300 * 2 ** min(failures - 1, 4))
                error = e.name if isinstance(e, ProviderError) else type(e).__name__
                if isinstance(e, ProviderError) and e.code in (401, 403):
                    state = "needs_connection"
                    delay = 3600
                else:
                    state = "backoff"
                if isinstance(e, ProviderError) and e.code == 429:
                    delay = max(900, delay, e.retry)
                ids = [
                    d["id"]
                    for d in self.devices()
                    if json.loads(d["config"]).get("connection") == con["id"] and d["active"]
                ]
                self.status(ids, state, error, delay)
            with self.db() as c:
                c.execute(
                    "UPDATE connections SET next_poll=?,state=?,error=?,failures=? WHERE id=?",
                    (time.time() + delay, state, error, failures, con["id"]),
                )

    def collect(self, stop, push):
        while not stop.is_set():
            try:
                self.apple(stop, push)
            except Exception as error:
                logging.warning("Apple collection loop failed (%s).", type(error).__name__)
            if stop.is_set():
                return
            try:
                self.hubble(push)
            except Exception as error:
                logging.warning("Hubble collection loop failed (%s).", type(error).__name__)
            stop.wait(2)


class UndiscoveredDevice(ValueError):
    """A Hubble packet names a device the last discovery didn't list."""


class ProviderError(Exception):
    def __init__(self, name, code=None, retry=None):
        self.name = name
        self.code = code
        try:
            self.retry = max(0, float(retry or 0))
        except (TypeError, ValueError):
            self.retry = 0
