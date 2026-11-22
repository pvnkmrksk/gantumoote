# Gantumoote

- **Flash history or local BLE.** Read [the Pi bench commands](README.md#flash-history-and-pi-ble). Start with `/flash` and `systemctl status gantumoote-ble`. Use `dashboard.ble capture` to check the actual time span and tag matches in an existing btmon file.
- **Program a tag.** Select its own prepared config and match the PCBA marking. Archive the NanoBeacon tool’s actual OTP output and verify the SHA-256 before sealing its ledger entry. See the [hardware programming guide](https://github.com/pvnkmrksk/gantu-tag/blob/main/docs/programming.md).
- **Secrets.** Keep per-tag configs, keys and programmer artifacts under the service’s private data directory. Sync source to GitHub and use the existing age-encrypted backup for private data. Confirm private routes reject unauthenticated requests before updating the Pi.
