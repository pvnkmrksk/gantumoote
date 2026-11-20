# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantu-tag, https://github.com/pvnkmrksk/gantu-tag (see NOTICE).
"""Paths and private file writes shared by the dashboard and its tools."""

import os
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
DATA = Path(os.environ.get("GANTU_DASHBOARD_DATA", HERE / "data")).expanduser().resolve()
PRIVATE = DATA / "secrets"
REGISTRY = "registry.sqlite3"
PUSH_DB = "notifications.sqlite3"
LOCAL_URL = "http://127.0.0.1:8766"


def public_url():
    path = DATA / "public-url.txt"
    return path.read_text().strip().rstrip("/") if path.is_file() else LOCAL_URL


def write_private(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
