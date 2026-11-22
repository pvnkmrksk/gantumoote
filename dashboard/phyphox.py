# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read the built-in Phyphox Light experiment and map its samples to wall time."""

import csv
import ipaddress
import json
import math
import os
import threading
import time
from bisect import bisect_right
from collections import deque
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

MAX_RESPONSE = 4 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def local_phone_url(value):
    parsed = urlsplit(value)
    try:
        address = ipaddress.IPv4Address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise ValueError("Use the local IPv4 URL displayed by Phyphox, including its port.") from None
    local_blocks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
    if (parsed.scheme != "http" or parsed.username is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or port == 0
            or not any(address in ipaddress.IPv4Network(block) for block in local_blocks)):
        raise ValueError("Use the local HTTP URL displayed by Phyphox.")
    return parsed.geturl().rstrip("/")


class LightClient:
    def __init__(self, base):
        self.base = local_phone_url(base)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.first_start = None
        self.last = None
        self.armed_at = time.time()

    def get(self, path, query=None):
        target = self.base + path
        if query:
            target += "?" + urlencode(query)
        with self.opener.open(target, timeout=1.5) as response:
            data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("Phone response is too large")
        return json.loads(data)

    def poll(self):
        starts = sorted(
            (float(event["experimentTime"]), float(event["systemTime"]))
            for event in self.get("/time") if event["event"].upper() == "START"
        )
        if not starts:
            self.first_start = self.last = None
            return "Press Start on the phone", []
        if starts[0] != self.first_start:
            self.first_start = starts[0]
            self.last = None
        query = {"illum_time": "full", "illum": "full"}
        if self.last is not None:
            threshold = format(self.last, ".17g")
            query = {"illum_time": threshold, "illum": f"{threshold}|illum_time"}
        response = self.get("/get", query)
        buffers = response["buffer"]
        if "illum_time" not in buffers or "illum" not in buffers:
            return "Open the Phyphox Light experiment", []
        intervals = buffers["illum_time"]["buffer"]
        illuminance = buffers["illum"]["buffer"]
        status = "Measuring" if response["status"]["measuring"] else "Paused on phone"
        if len(intervals) != len(illuminance):
            return status, []
        experiment_times = [item[0] for item in starts]
        samples = []
        for elapsed, lux in zip(intervals, illuminance):
            if elapsed is None or not math.isfinite(elapsed) or (self.last is not None and elapsed <= self.last):
                continue
            self.last = elapsed
            index = bisect_right(experiment_times, elapsed) - 1
            if index < 0 or lux is None or not math.isfinite(lux):
                continue
            anchor_elapsed, anchor_system = starts[index]
            epoch = anchor_system + (elapsed - anchor_elapsed)
            if epoch >= self.armed_at - 2.0:
                samples.append((epoch, elapsed, lux))
        return status, samples


def timestamp(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


class LightFeed:
    def __init__(self, client, path=None, *, plain=False):
        self.client = client
        self.path = path
        self.plain = plain
        self.status = "Connecting to phone"
        self.samples = deque(maxlen=4096)
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.run, name="phyphox-light", daemon=True)

    def snapshot(self):
        with self.lock:
            return self.status, tuple(self.samples)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.worker.join(timeout=3.5)

    def run(self):
        try:
            if self.path is None:
                self.collect()
                return
            file = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(file, "w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, delimiter="\t")
                writer.writerow(("timestamp", "lux", "experiment_s"))
                stream.flush()
                self.collect(stream, writer)
        except OSError:
            with self.lock:
                self.status = "Unable to write the light log"

    def collect(self, stream=None, writer=None):
        while not self.stopped.is_set():
            try:
                status, samples = self.client.poll()
            except (OSError, ValueError, TypeError, KeyError, OverflowError):
                status, samples = "Waiting for phone · check Phyphox remote access", []
            with self.lock:
                self.status = status
                self.samples.extend(samples)
            for epoch, elapsed, lux in samples:
                observed = timestamp(epoch)
                if writer is not None:
                    writer.writerow((observed, lux, f"{elapsed:.6f}"))
                if self.plain:
                    print(f"{observed}  phone  light {lux:g} lx", flush=True)
            if writer is not None and samples:
                stream.flush()
            self.stopped.wait(1)
