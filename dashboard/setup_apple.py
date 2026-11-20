# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantu-tag, https://github.com/pvnkmrksk/gantu-tag (see NOTICE).
"""Install the public Apple authentication helper and extract a verified CoreADI library."""

import gzip
import hashlib
import os
import shutil
import stat
import struct
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen

from .apple import COREADI, FINDMY, MACHOADI
from .settings import DATA, write_private

EMULATOR = "https://github.com/sacca97/os-findmy-e.git"
EMULATOR_REVISION = "d4d2a832baf47d1cf43d989879950ac2aec45f47"
APPLE_INSTALLER = (
    "https://secure-appldnld.apple.com/itunes12/"
    "041-56365-20210413-79A5AC6D-6B0A-46EC-B399-5A381183E3BE/iTunes12.8.3.dmg"
)
INSTALLER_SHA = "dd08b9967db3e5847955fa168ce880303abf5f36de12abb2061e7ef1a25be214"
LIBRARY_SHA = "2eafc3c79c7f41a8afbfbeaaf23ee5dab5ad5bda55ec8e54dbc0ddf1d20ce630"
COREADI_PATH = "System/Library/PrivateFrameworks/CoreADI.framework/Versions/A/CoreADI"


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_library(payload):
    """Read the checksummed odc CPIO payload and return the x86-64 Mach-O slice."""
    data = gzip.decompress(payload)
    offset = 0
    while offset < len(data):
        if data[offset : offset + 6] != b"070707":
            raise ValueError("Unexpected CoreADI archive format")
        mode = int(data[offset + 18 : offset + 24], 8)
        name_size = int(data[offset + 59 : offset + 65], 8)
        size = int(data[offset + 65 : offset + 76], 8)
        offset += 76
        name = data[offset : offset + name_size - 1].decode()
        offset += name_size
        if name.removeprefix("./") == COREADI_PATH and stat.S_ISREG(mode):
            library = data[offset : offset + size]
            if library[:4] != b"\xca\xfe\xba\xbe":
                raise ValueError("Expected a fat Mach-O library")
            (count,) = struct.unpack_from(">I", library, 4)
            for i in range(count):
                cpu, _, start, length, _ = struct.unpack_from(">IIIII", library, 8 + i * 20)
                if cpu == 0x01000007:
                    return library[start : start + length]
        offset += size
    raise ValueError("CoreADI x86-64 library not found")


def checkout(repo, url, revision, marker):
    if repo.exists():
        if not marker.is_file():
            raise SystemExit(f"Move {repo} aside and rerun setup.")
        actual = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision:
            raise SystemExit(f"Move {repo} aside and rerun setup.")
        return
    with tempfile.TemporaryDirectory(dir=DATA) as directory:
        checkout = Path(directory) / "source"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "fetch", "-q", "--depth=1", url, revision], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True)
        checkout.rename(repo)


def setup_library():
    if COREADI.is_file() and checksum(COREADI) == LIBRARY_SHA:
        return
    seven_zip = shutil.which("7zz") or shutil.which("7z")
    if not seven_zip:
        raise SystemExit("Install 7-Zip (7zz or 7z) before running Apple setup.")
    print("Downloading Apple's iTunes installer to extract CoreADI (about 290 MB).", flush=True)
    with tempfile.TemporaryDirectory(dir=DATA) as directory:
        temp = Path(directory)
        installer = temp / "iTunes.dmg"
        with urlopen(APPLE_INSTALLER, timeout=120) as source, installer.open("wb") as target:
            shutil.copyfileobj(source, target)
        if checksum(installer) != INSTALLER_SHA:
            raise ValueError("Apple installer checksum mismatch")
        for archive, item in (
            (installer, "iTunes/Install iTunes.pkg"),
            (temp / "iTunes/Install iTunes.pkg", "CoreADI.pkg/Payload"),
        ):
            subprocess.run(
                [seven_zip, "x", str(archive), item, "-o" + directory, "-y"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        library = extract_library((temp / "CoreADI.pkg/Payload").read_bytes())
        if hashlib.sha256(library).hexdigest() != LIBRARY_SHA:
            raise ValueError("CoreADI library checksum mismatch")
        write_private(COREADI, library)


def main():
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkout(
        FINDMY,
        "https://github.com/parawanderer/FindMy.py.git",
        "7969003ca785658369b650f75d9e7ca519566bf6",
        FINDMY / "findmy/reports/account.py",
    )
    checkout(DATA / "os-findmy-e", EMULATOR, EMULATOR_REVISION, MACHOADI / "provider.py")
    setup_library()
    print("Apple setup ready. Run python -m dashboard.apple login.")


if __name__ == "__main__":
    main()
