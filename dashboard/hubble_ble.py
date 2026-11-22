# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authenticate Hubble v2 BLE advertisements using their device keys.

The Hubble EID and AES-EAX calculations follow the Gantu NanoBeacon decoder
(gantu-tag/tools/hubble_decode.py, Pavan Kumar Kaushik).
"""

import hmac
import logging
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC


def aes_block(key, block):
    return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(block)


def eax_mac(key, label, content):
    mac = CMAC(algorithms.AES(key))
    mac.update(bytes(15) + bytes((label,)) + content)
    return mac.finalize()


@lru_cache(maxsize=64)
def rotating_ids(key, exponent):
    ids = {}
    for period in range(min(128, 1 << (32 - exponent))):
        counter = (period << exponent).to_bytes(4, "big")
        derived = aes_block(key, bytes(11) + b"\xff\x00\x00" + counter[:2])
        eid = aes_block(derived, bytes(11) + bytes((exponent,)) + counter)[:8]
        ids[eid[::-1]] = counter
    return ids


def hubble_identities(directory):
    identities = {}
    for path in directory.glob("*.hubble.key"):
        tag = path.name.split(".", 1)[0].replace("_", "-").capitalize()
        if not (tag.startswith("Hagalu-") or tag.startswith("Irulu-")):
            continue
        try:
            values = dict(line.split(": ", 1) for line in path.read_text().splitlines()
                          if ": " in line and not line.startswith("#"))
            key = bytes.fromhex(values["key_hex"])
            exponent = int(values["period_exponent"])
            if len(key) != 16 or not 0 <= exponent <= 31:
                raise ValueError("Invalid key or period exponent")
        except (OSError, KeyError, ValueError):
            logging.warning("Invalid Hubble identity for %s", tag)
            continue
        for eid, counter in rotating_ids(key, exponent).items():
            identities.setdefault(eid, []).append((tag, key, counter))
    return identities


def match_hubble(identities, data):
    if len(data) < 15 or data[0] >> 2 != 2:
        return None
    for tag, key, counter in identities.get(data[3:11], ()):
        nonce = eax_mac(key, 0, counter + data[1:3])
        header = eax_mac(key, 1, b"")
        message = eax_mac(key, 2, data[11:-4])
        expected = bytes(a ^ b ^ c for a, b, c in zip(nonce[:4], header[:4], message[:4]))
        if hmac.compare_digest(expected, data[-4:]):
            return tag
    return None
