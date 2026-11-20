"""Write local SQLite snapshots, including any committed data in the write-ahead logs."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .settings import DATA, PUSH_DB, REGISTRY, write_private


def snapshot(path, target):
    temporary = target.with_suffix(".tmp")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = DATA / "backups"
    folder.mkdir(exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    index_file = folder / "database-index.json"
    index = json.loads(index_file.read_text()) if index_file.is_file() else {}
    prefixes = []

    def store(path):
        if not path.is_file() or not path.resolve().is_relative_to(DATA):
            return None
        relative = path.relative_to(DATA).as_posix()
        prefix = (
            path.stem
            if path.parent == DATA
            else "device-" + hashlib.sha256(relative.encode()).hexdigest()[:12]
        )
        target = folder / f"{prefix}-{stamp}.sqlite3"
        snapshot(path, target)
        index[prefix] = relative
        prefixes.append(prefix)
        return target

    # Snapshot the registry before its histories so a backed-up cursor never outruns them.
    registry = store(DATA / REGISTRY)
    if registry:
        with closing(sqlite3.connect(registry)) as db:
            devices = db.execute("SELECT config FROM devices").fetchall()
        for (encoded,) in devices:
            database = json.loads(encoded).get("database")
            if database:
                store(DATA / database)
    store(DATA / PUSH_DB)
    write_private(index_file, json.dumps(index, indent=2).encode() + b"\n")
    for prefix in set(prefixes):
        for old in sorted(folder.glob(prefix + "-*.sqlite3"))[:-14]:
            old.unlink()
    print(f"Saved {len(prefixes)} SQLite snapshots to {folder}.")


if __name__ == "__main__":
    main()
