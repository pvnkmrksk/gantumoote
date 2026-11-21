# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantumoote, https://github.com/pvnkmrksk/gantumoote (see NOTICE).
"""Cloud backup against a local bare repository, with a stand-in for age that copies its input."""

import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from . import cloud_backup


class CloudBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data, self.remote, bin_dir = base / "data", base / "remote.git", base / "bin"
        bin_dir.mkdir()
        fake_age = bin_dir / "age"  # age -r RECIPIENT -o OUT  <stdin>
        fake_age.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\ncat > "$out"\n')
        fake_age.chmod(0o755)
        self.path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}:{self.path}"
        subprocess.run(["git", "init", "--quiet", "--bare", str(self.remote)], check=True)
        (self.data / "secrets/devices").mkdir(parents=True)
        (self.data / "secrets/devices/tag.b64").write_text("key")
        (self.data / "public-url.txt").write_text("https://example.org\n")
        for skipped in ("backups/old.sqlite3", "FindMy.py/x.py", "os-findmy-e/y.py", "CoreADI.x86_64"):
            (self.data / skipped).parent.mkdir(parents=True, exist_ok=True)
            (self.data / skipped).write_text("skip")
        self.db = sqlite3.connect(self.data / "registry.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE t(x)")
        self.db.execute("INSERT INTO t VALUES (1)")
        self.db.commit()  # stays in the WAL while the connection is open
        (self.data / cloud_backup.CONFIG).write_text(json.dumps({"repo": str(self.remote), "recipient": "age1test"}))

    def tearDown(self):
        self.db.close()
        os.environ["PATH"] = self.path
        self.tmp.cleanup()

    def pushed(self):
        log = subprocess.run(["git", "-C", str(self.remote), "log", "--format=%s", "main"], capture_output=True, text=True)
        return log.stdout.split("\n")[:-1] if log.returncode == 0 else []

    def archive(self):
        blob = subprocess.run(["git", "-C", str(self.remote), "show", f"main:{cloud_backup.ARCHIVE}"], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            names = tar.getnames()
            registry = tar.extractfile("registry.sqlite3").read()
        return names, registry

    def test_pushes_only_changes_and_skips_reproducible_files(self):
        self.assertTrue(cloud_backup.run(self.data))
        names, registry = self.archive()
        self.assertEqual(sorted(names), ["cloud-backup.json", "public-url.txt", "registry.sqlite3", "secrets/devices/tag.b64"])
        restored = Path(self.tmp.name) / "restored.sqlite3"
        restored.write_bytes(registry)
        self.assertEqual(sqlite3.connect(restored).execute("SELECT x FROM t").fetchall(), [(1,)])
        self.assertFalse(cloud_backup.run(self.data))
        self.assertEqual(len(self.pushed()), 1)
        self.db.execute("INSERT INTO t VALUES (2)")
        self.db.commit()
        self.assertTrue(cloud_backup.run(self.data))
        self.assertEqual(len(self.pushed()), 2)


if __name__ == "__main__":
    unittest.main()
