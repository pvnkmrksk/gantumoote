# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantu-tag, https://github.com/pvnkmrksk/gantu-tag (see NOTICE).
"""Sign in locally or fetch a batch of OpenHaystack tags using the saved session."""

import argparse
import getpass
import json
import logging
import os
import subprocess
import sys

from .settings import DATA, PRIVATE

SESSION = PRIVATE / "apple-session.json"
IDENTITY = PRIVATE / "apple-identity"
MACHOADI = DATA / "os-findmy-e/tools/machoadi"
FINDMY = DATA / "FindMy.py"
COREADI = DATA / "CoreADI.x86_64"


class AppleLoginRequired(Exception):
    pass


def prepare_imports():
    if not COREADI.is_file() or not (MACHOADI / "provider.py").is_file() or not (FINDMY / "findmy").is_dir():
        raise AppleLoginRequired()
    os.environ["COREADI_PATH"] = str(COREADI)
    sys.path[:0] = [str(FINDMY), str(MACHOADI)]


def apple_account(state=None):
    from findmy import AppleAccount
    from provider import MacAnisetteProvider

    if not (IDENTITY / "identity.json").is_file() and state is None:
        subprocess.run(
            [sys.executable, str(MACHOADI / "mac_anisette.py"), "provision", "--state", str(IDENTITY)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
    provider = MacAnisetteProvider(IDENTITY)
    identity = provider.identity_json
    if state is not None:
        if state["account"].get("password") or state["ids"]["devid"] != identity["device_id"]:
            raise AppleLoginRequired()
        return AppleAccount(provider, state_info=state, timeout=45)

    account = AppleAccount(
        provider,
        uid=identity["local_user_uuid"],
        devid=identity["device_id"],
        timeout=45,
    )
    request = account._asyncacc._gsa_request

    async def compatible_request(parameters):
        response = await request(parameters)
        status = response.get("Status", {})
        # Some Apple responses omit the challenge name for a trusted-device login.
        if parameters.get("o") == "complete" and status.get("hsc") == 409 and not status.get("au"):
            status["au"] = "trustedDeviceSecondaryAuth"
        return response

    account._asyncacc._gsa_request = compatible_request
    return account


def sign_in():
    prepare_imports()
    from findmy import LoginState

    account = apple_account()
    try:
        email = input("Apple account email: ").strip()
        password = getpass.getpass("Apple account password: ")
        state = account.login(email, password)
        del password
        if state == LoginState.REQUIRE_2FA:
            print("Approve sign-in on your trusted Apple device.")
            try:
                account.td_2fa_request()
            except Exception:
                print("If the prompt did not arrive, use Get Verification Code on your trusted device.")
            account.td_2fa_submit(getpass.getpass("Verification code: ").strip())
        if account.login_state != LoginState.LOGGED_IN:
            raise AppleLoginRequired()
        from login_store import save

        save(account._asyncacc, SESSION, keep_password=False)
        SESSION.chmod(0o600)
        print("Signed in. The password-free session was saved locally.")
    finally:
        account._evt_loop.run_until_complete(account.close())


def fetch():
    account = None
    status = retry_after = None
    try:
        if not SESSION.is_file() or not (IDENTITY / "identity.json").is_file():
            raise AppleLoginRequired()
        prepare_imports()
        account = apple_account(json.loads(SESSION.read_text()))
        from findmy import KeyPair

        request = account._asyncacc._http.post

        async def observed_post(*args, **kwargs):
            nonlocal status, retry_after
            response = await request(*args, **kwargs)
            status = response.status_code
            retry_after = response.headers.get("Retry-After")
            return response

        account._asyncacc._http.post = observed_post
        devices = json.load(sys.stdin)
        keys = [KeyPair.from_b64((DATA / d["key_file"]).read_text().strip()) for d in devices]
        batch = account.fetch_location_history(keys)
        return {
            "ok": True,
            "devices": {
                device["id"]: [
                    {
                        "timestamp": report.timestamp.isoformat(),
                        "latitude": report.latitude,
                        "longitude": report.longitude,
                        "accuracy_m": report.horizontal_accuracy,
                    }
                    for report in batch.get(key, [])
                ]
                for device, key in zip(devices, keys)
            },
        }
    except (Exception, SystemExit) as exc:
        return {"ok": False, "error": type(exc).__name__, "http_status": status, "retry_after": retry_after}
    finally:
        if account is not None:
            account._evt_loop.run_until_complete(account.close())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "fetch"])
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.CRITICAL)
    if args.command == "fetch":
        print(json.dumps(fetch()))
        return
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        sign_in()
    except AppleLoginRequired:
        parser.exit(1, "Apple sign-in unavailable. Run the Apple setup and try signing in again.\n")
    except (Exception, SystemExit) as exc:
        parser.exit(
            1, f"Apple sign-in stopped ({type(exc).__name__}). Check the setup and trusted-device sign-in.\n"
        )


if __name__ == "__main__":
    main()
