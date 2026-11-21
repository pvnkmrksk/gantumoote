#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantumoote, https://github.com/pvnkmrksk/gantumoote (see NOTICE).
# Install boot services using the dashboard's existing Python environment.
set -eu
umask 077
cd "$(dirname "$0")/../.."
root=$(pwd -P)
user=$(id -un)
[ "$(uname -s)" = Linux ] && [ "$(id -u)" -ne 0 ] || {
    echo 'Run this as your regular user on a Linux host with systemd.' >&2
    exit 1
}
# Paths are written literally into the systemd units.
case "$root" in *[!a-zA-Z0-9_./-]*)
    echo 'Use a checkout path without spaces or shell characters.' >&2; exit 1 ;;
esac
test -x dashboard/.venv/bin/python || {
    echo 'Create dashboard/.venv and install dashboard/requirements.txt first.' >&2; exit 1
}
dashboard/.venv/bin/python -c 'import cryptography, markdown, pywebpush'
units=$(mktemp -d)
trap 'rm -rf "$units"' EXIT
cat > "$units/gantumoote.service" <<UNIT
[Unit]
Description=Gantumoote dashboard and tag collector
Wants=network-online.target
After=network-online.target

[Service]
User=$user
WorkingDirectory=$root
ExecStart=$root/dashboard/.venv/bin/python -m dashboard.server
Restart=always
RestartSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
cat > "$units/gantumoote-backup.service" <<UNIT
[Unit]
Description=Back up local Gantumoote SQLite history

[Service]
Type=oneshot
User=$user
WorkingDirectory=$root
ExecStart=$root/dashboard/.venv/bin/python -m dashboard.backup
UMask=0077
UNIT
cat > "$units/gantumoote-backup.timer" <<'UNIT'
[Unit]
Description=Daily Gantumoote backup

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
UNIT
if [ -f dashboard/data/cloudflared.yml ]; then
    cloudflared=$(command -v cloudflared) || {
        echo 'Install cloudflared before enabling your configured tunnel.' >&2; exit 1
    }
    cloudflared tunnel --config "$root/dashboard/data/cloudflared.yml" ingress validate
    cat > "$units/gantumoote-tunnel.service" <<UNIT
[Unit]
Description=Gantumoote named Cloudflare Tunnel
Wants=network-online.target gantumoote.service
After=network-online.target gantumoote.service

[Service]
User=$user
WorkingDirectory=$root
ExecStart=$cloudflared tunnel --no-autoupdate --config $root/dashboard/data/cloudflared.yml run
Restart=always
RestartSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
fi
if [ -f dashboard/data/cloud-backup.json ]; then
    command -v age >/dev/null || { echo 'Install age (sudo apt install age) for the cloud backup.' >&2; exit 1; }
    cat > "$units/gantumoote-cloud-backup.service" <<UNIT
[Unit]
Description=Push an encrypted Gantumoote backup to its private Git repository
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$user
WorkingDirectory=$root
ExecStart=$root/dashboard/.venv/bin/python -m dashboard.cloud_backup
UMask=0077
UNIT
    cat > "$units/gantumoote-cloud-backup.timer" <<'UNIT'
[Unit]
Description=Daily encrypted Gantumoote cloud backup

[Timer]
OnCalendar=*-*-* 03:40
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
fi
sudo install -m 644 "$units/"* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gantumoote.service gantumoote-backup.timer
sudo systemctl restart gantumoote.service
if [ -f "$units/gantumoote-tunnel.service" ]; then
    sudo systemctl enable --now gantumoote-tunnel.service
    sudo systemctl restart gantumoote-tunnel.service
fi
if [ -f "$units/gantumoote-cloud-backup.timer" ]; then
    sudo systemctl enable --now gantumoote-cloud-backup.timer
fi
echo 'Gantumoote is enabled at boot. Run dashboard/.venv/bin/python -m dashboard.check to verify.'
