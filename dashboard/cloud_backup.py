# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantumoote, https://github.com/pvnkmrksk/gantumoote (see NOTICE).
"""Push an encrypted copy of the dashboard's data to a private Git repository.

Configured by DATA/cloud-backup.json, for example
    {"repo": "git@github.com:you/gantumoote-backup.git", "recipient": "age1..."}
with a deploy key that can write to that one repository in DATA/secrets/cloud-backup-key and the
server's host keys pinned in DATA/secrets/github-known-hosts.

Each run snapshots every SQLite database (live write-ahead logs included), adds secrets/,
public-url.txt and cloudflared.yml, packs them into one tar.gz and encrypts it with `age` to the
recipient. It commits gantumoote-data.tar.gz.age only when the contents changed. The repository
only ever holds ciphertext; whoever has the matching age identity restores with standard tools:

    age -d -i identity.txt gantumoote-data.tar.gz.age | tar -xz -C dashboard/data
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .backup import snapshot
from .settings import DATA

CONFIG = "cloud-backup.json"
ARCHIVE = "gantumoote-data.tar.gz.age"
DIGEST = "content-sha256.txt"
# Local snapshots, the working clone, and what setup_apple downloads again stay out of the backup.
SKIP_DIRS = {"backups", "cloud-backup", "FindMy.py", "os-findmy-e"}
SKIP_FILES = {"CoreADI.x86_64"}


def collect(root, staging):
    """Copy the data folder into staging, SQLite via the backup API. Returns sorted relative paths."""
    paths = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in SKIP_DIRS or path.name in SKIP_FILES or not path.is_file():
            continue
        if path.name.endswith(("-wal", "-shm", ".tmp")):
            continue
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.suffix == ".sqlite3":
            snapshot(path, target)
        else:
            shutil.copyfile(path, target)
        paths.append(rel)
    return paths


def digest(staging, paths):
    h = hashlib.sha256()
    for rel in paths:
        data = (staging / rel).read_bytes()
        h.update(f"{rel.as_posix()}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


def pack(staging, paths):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for rel in paths:
            info = tar.gettarinfo(staging / rel, arcname=rel.as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(staging / rel, "rb") as f:
                tar.addfile(info, f)
    return buffer.getvalue()


def encrypt(plaintext, recipient, target):
    age = shutil.which("age")
    if not age:
        raise SystemExit("Install age first (sudo apt install age, or brew install age).")
    subprocess.run([age, "-r", recipient, "-o", str(target)], input=plaintext, check=True)


def git(clone, *args, env=None, capture=False):
    result = subprocess.run(
        ["git", "-C", str(clone), *args], check=True, env=env, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


def ssh_env(root):
    key, known = root / "secrets/cloud-backup-key", root / "secrets/github-known-hosts"
    env = dict(os.environ)
    if key.is_file():
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes"
            f" -o UserKnownHostsFile={known} -o StrictHostKeyChecking=yes"
        )
    return env


def run(root=DATA, push=True):
    config = json.loads((root / CONFIG).read_text())
    repo, recipient = config["repo"], config["recipient"]
    branch = config.get("branch", "main")
    env = ssh_env(root)
    clone = root / "cloud-backup/repo"
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(["git", "clone", "--quiet", repo, str(clone)], check=True, env=env)
    elif push:
        git(clone, "fetch", "--quiet", "origin", env=env)
        if git(clone, "ls-remote", "--heads", "origin", branch, env=env, capture=True):
            git(clone, "reset", "--quiet", "--hard", f"origin/{branch}")
    with tempfile.TemporaryDirectory(dir=clone.parent) as tmp:
        staging = Path(tmp)
        paths = collect(root, staging)
        content = digest(staging, paths)
        previous = clone / DIGEST
        if previous.is_file() and previous.read_text().strip() == content:
            print(f"Cloud backup unchanged ({len(paths)} files); nothing to push.")
            return False
        plaintext = pack(staging, paths)
    encrypt(plaintext, recipient, clone / ARCHIVE)
    (clone / DIGEST).write_text(content + "\n")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    git(clone, "add", ARCHIVE, DIGEST)
    git(
        clone, "-c", "user.name=Gantumoote backup", "-c", f"user.email=gantumoote@{socket.gethostname()}.local",
        "commit", "--quiet", "-m", f"Backup {stamp}: {len(paths)} files, {len(plaintext) // 1024} KiB compressed",
    )
    if push:
        git(clone, "push", "--quiet", "origin", f"HEAD:{branch}", env=env)
    size = (clone / ARCHIVE).stat().st_size
    print(f"Cloud backup {'pushed' if push else 'committed locally'}: {len(paths)} files, {size // 1024} KiB encrypted.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-push", action="store_true", help="commit to the local clone only")
    args = parser.parse_args()
    os.umask(0o077)
    if not (DATA / CONFIG).is_file():
        raise SystemExit(f"No {DATA / CONFIG}; see the README's cloud backup section.")
    run(push=not args.no_push)


if __name__ == "__main__":
    main()
