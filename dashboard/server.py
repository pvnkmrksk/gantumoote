# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantu-tag, https://github.com/pvnkmrksk/gantu-tag (see NOTICE).
"""Run the private dashboard and provider collector on loopback."""

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import signal
import sqlite3
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .guide_view import guide_html, GUIDES
from .portal import ClosingConnection, Portal
from .settings import DATA, HERE, LOCAL_URL, PRIVATE, PUSH_DB, public_url, write_private

COOKIE_NAME = "gantu_session"
ASSETS = {
    "maplibre.css": "text/css",
    "maplibre.js": "application/javascript",
    "trajectory.js": "application/javascript",
    "ui.js": "application/javascript",
    "devices.js": "application/javascript",
    "ui.css": "text/css",
}
ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="36" fill="#141119"/>
<circle cx="96" cy="96" r="47" fill="none" stroke="#ffb36a" stroke-width="6"/>
<circle cx="96" cy="96" r="15" fill="#ed8129"/></svg>"""


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.portal = Portal(DATA)
        PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        access_file = PRIVATE / "access-code.txt"
        if not access_file.is_file():
            write_private(access_file, secrets.token_urlsafe(32).encode() + b"\n")
        self.access = access_file.read_text().strip()
        self.session = hmac.new(self.access.encode(), b"gantu-browser-session", hashlib.sha256).hexdigest()
        self.vapid_key = PRIVATE / "vapid.pem"
        if not self.vapid_key.is_file():
            key = ec.generate_private_key(ec.SECP256R1())
            write_private(
                self.vapid_key,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
        key = serialization.load_pem_private_key(self.vapid_key.read_bytes(), password=None)
        public = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.push_key = base64.urlsafe_b64encode(public).decode().rstrip("=")
        with self.push_db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions(endpoint TEXT PRIMARY KEY, subscription TEXT NOT NULL)"
            )

    def push_db(self):
        return sqlite3.connect(DATA / PUSH_DB, timeout=15, factory=ClosingConnection)

    def history(self, device=None):
        data = self.portal.history(device)
        with self.push_db() as db:
            data["collector"]["subscribers"] = db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        return data

    def push(self, title, body):
        with self.push_db() as db:
            subscriptions = db.execute("SELECT endpoint, subscription FROM subscriptions").fetchall()
        delivered = 0
        for endpoint, subscription in subscriptions:
            try:
                webpush(
                    subscription_info=json.loads(subscription),
                    data=json.dumps({"title": title, "body": body}),
                    vapid_private_key=str(self.vapid_key),
                    vapid_claims={"sub": public_url()},
                    timeout=10,
                    ttl=3600,
                )
                delivered += 1
            except WebPushException as error:
                if error.response is not None and error.response.status_code in (404, 410):
                    with self.push_db() as db:
                        db.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
            except Exception:
                logging.warning("A browser notification could not be delivered.")
        return delivered


class Handler(BaseHTTPRequestHandler):
    server_version = "GantuDashboard"
    sys_version = ""

    def log_message(self, *_):
        # Access logs can contain tracker paths. Keep only startup and operational messages.
        pass

    def send(self, code, body, kind="application/json", headers=None):
        if kind in ("application/json", "application/manifest+json"):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def bearer(self):
        value = self.headers.get("Authorization", "")
        return hmac.compare_digest(value.encode(), ("Bearer " + self.server.access).encode())

    def authorized(self):
        if self.bearer():
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE_NAME)
        except CookieError:
            return False
        return cookie is not None and hmac.compare_digest(cookie.value.encode(), self.server.session.encode())

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        if path == "/manifest.json":
            return self.send(
                200,
                {
                    "id": "/",
                    "name": "Gantu dashboard",
                    "short_name": "Gantu",
                    "start_url": "/",
                    "display": "standalone",
                    "background_color": "#141119",
                    "theme_color": "#141119",
                    "icons": [
                        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
                    ],
                },
                "application/manifest+json",
            )
        if path == "/icon.svg":
            return self.send(200, ICON, "image/svg+xml")
        if path == "/sw.js":
            return self.send(200, (HERE / "sw.js").read_bytes(), "application/javascript")
        if not self.authorized():
            if path == "/":
                return self.send(200, (HERE / "login.html").read_bytes(), "text/html; charset=utf-8")
            return self.send(401, {"error": "Sign in required"})

        if path == "/":
            boot = json.dumps({"access": self.server.access, "history": self.server.history()}).replace(
                "<", "\\u003c"
            )
            page = (HERE / "ui.html").read_text().replace("__BOOT_JSON__", boot)
            return self.send(200, page, "text/html; charset=utf-8")
        if path.startswith("/assets/"):
            name = path.removeprefix("/assets/")
            if name in ASSETS:
                return self.send(200, (HERE / "assets" / name).read_bytes(), ASSETS[name])
        if path.startswith("/guides/"):
            name = path.removeprefix("/guides/")
            if name in GUIDES:
                return self.send(200, guide_html(name), "text/html; charset=utf-8")
        if path == "/api/devices":
            return self.send(200, self.server.portal.inventory())
        if path == "/api/push-config":
            return self.send(200, {"public_key": self.server.push_key})
        if path in ("/api/history", "/api/packets", "/history.csv"):
            device = parse_qs(url.query).get("device", [None])[0]
            try:
                if path == "/api/packets":
                    return self.send(200, self.server.portal.packets(device))
                history = self.server.history(device)
            except ValueError:
                return self.send(404, {"error": "Device not found"})
            if path == "/api/history":
                return self.send(200, history)
            with io.StringIO() as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=["timestamp", "latitude", "longitude", "accuracy_m"]
                )
                writer.writeheader()
                writer.writerows(history["reports"])
                return self.send(
                    200,
                    stream.getvalue(),
                    "text/csv",
                    {
                        "Content-Disposition": 'attachment; filename="gantu-history.csv"',
                    },
                )
        return self.send(404, {})

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
            raise ValueError("Expected a JSON body up to 16 KiB")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def subscribe(self, data):
        endpoint = data.get("endpoint", "")
        host = urlsplit(endpoint)
        hostname = host.hostname or ""
        keys = data.get("keys", {})
        allowed = hostname.endswith(".push.apple.com") or hostname in (
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
        )
        if host.scheme != "https" or not allowed or host.port not in (None, 443):
            raise ValueError("Unsupported push service")
        if not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("Missing subscription keys")
        with self.server.push_db() as db:
            db.execute("INSERT OR REPLACE INTO subscriptions VALUES(?,?)", (endpoint, json.dumps(data)))
        return {"ok": True}

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/session":
            if not self.bearer():
                return self.send(401, {"error": "Invalid access code"})
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            cookie = f"{COOKIE_NAME}={self.server.session}; HttpOnly; SameSite=Lax; Path=/; Max-Age=43200{secure}"
            return self.send(200, {}, headers={"Set-Cookie": cookie})

        portal = self.server.portal
        actions = {
            "/api/devices": portal.add,
            "/api/devices/update": portal.toggle,
            "/api/devices/rename": portal.rename,
            "/api/devices/delete": portal.delete,
            "/api/devices/hubble-key": portal.save_hubble_key,
            "/api/connections/hubble": portal.connect,
        }
        if path in actions:
            if not self.bearer():
                return self.send(401, {"error": "Sign in required"})
            try:
                return self.send(200, actions[path](self.read_json()))
            except ValueError as error:
                return self.send(400, {"error": str(error)})
            except (KeyError, TypeError):
                return self.send(400, {"error": "Invalid request"})
            except Exception:
                return self.send(500, {"error": "Could not save dashboard settings"})

        if not self.authorized():
            return self.send(401, {"error": "Sign in required"})
        if path == "/api/subscribe":
            try:
                return self.send(200, self.subscribe(self.read_json()))
            except (AttributeError, TypeError, ValueError):
                return self.send(400, {"error": "Invalid subscription"})
        if path == "/api/test-push":
            delivered = self.server.push("Gantu dashboard", "Notifications are working.")
            return self.send(200 if delivered else 409, {"delivered": delivered})
        return self.send(404, {})


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    stop = threading.Event()
    origin = urlsplit(LOCAL_URL)
    with Dashboard((origin.hostname, origin.port)) as server:
        worker = threading.Thread(target=server.portal.collect, args=(stop, server.push), daemon=True)
        worker.start()

        def shutdown(*_):
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print(f"Gantu dashboard at {LOCAL_URL}. Access code file: {PRIVATE / 'access-code.txt'}", flush=True)
        try:
            server.serve_forever()
        finally:
            stop.set()


if __name__ == "__main__":
    main()
