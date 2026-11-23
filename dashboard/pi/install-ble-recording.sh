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
unit=$(mktemp)
trap 'rm -f "$unit"' EXIT
cat > "$unit" <<UNIT
[Unit]
Description=Gantumoote registered-tag BLE recording
Wants=bluetooth.service
After=bluetooth.service

[Service]
User=$user
WorkingDirectory=$root
ExecStart=$root/dashboard/pi/gantu-ble --record-known
Restart=always
RestartSec=10
RuntimeMaxSec=1d
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
StandardOutput=null
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
sudo install -m 0644 "$unit" /etc/systemd/system/gantumoote-ble-recording.service
sudo systemctl daemon-reload
sudo systemctl enable --now gantumoote-ble-recording.service
