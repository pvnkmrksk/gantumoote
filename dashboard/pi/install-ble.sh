#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu
cd "$(dirname "$0")/../.."
root=$(pwd -P)
user=$(id -un)
[ "$(uname -s)" = Linux ] && [ "$(id -u)" -ne 0 ] || {
    echo 'Run as the Gantumoote user on the Linux host.' >&2
    exit 1
}
case "$root" in *[!a-zA-Z0-9_./-]*) exit 1;; esac
/usr/bin/python3 -c 'import dbus; from gi.repository import GLib' || {
    echo 'Install python3-dbus and python3-gi.' >&2
    exit 1
}
unit=$(mktemp)
trap 'rm -f "$unit"' EXIT
cat > "$unit" <<UNIT
[Unit]
Description=Gantumoote Pi BLE monitor
Requires=bluetooth.service
After=bluetooth.service

[Service]
User=$user
WorkingDirectory=$root
ExecStart=/usr/bin/python3 -m dashboard.ble monitor
Restart=always
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
sudo install -m 0644 "$unit" /etc/systemd/system/gantumoote-ble.service
sudo systemctl daemon-reload
sudo systemctl enable --now gantumoote-ble.service
