"""Check the dashboard without printing private keys or locations."""

import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .settings import LOCAL_URL, PRIVATE, public_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", action="store_true", help="check the configured HTTPS address")
    args = parser.parse_args()
    origin = public_url() if args.public else LOCAL_URL
    if args.public and origin == LOCAL_URL:
        parser.error("Set the public address in dashboard/data/public-url.txt first.")
    path = origin + "/api/devices"
    headers = {"User-Agent": "GantuDashboard/1.0"}
    try:
        with urlopen(Request(path, headers=headers), timeout=15):
            raise SystemExit("ERROR: device inventory is available without signing in")
    except HTTPError as error:
        if error.code not in (401, 403):
            raise
    access = (PRIVATE / "access-code.txt").read_text().strip()
    headers["Authorization"] = "Bearer " + access
    with urlopen(Request(path, headers=headers), timeout=15) as response:
        inventory = json.load(response)
    print(f"{origin}: authentication OK")
    for item in inventory["devices"]:
        print(
            f"  {item['name']} ({item['provider']}): {item['state']}; "
            f"{item['report_count']} sightings, {item['packet_count']} packets"
        )
    for connection in inventory["connections"]:
        print(f"  Hubble connection: {connection['state']}")
    if not inventory["devices"]:
        print("  No devices yet. Add one from Devices & connections.")


if __name__ == "__main__":
    main()
