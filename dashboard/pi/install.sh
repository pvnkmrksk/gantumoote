#!/bin/sh
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
cat > "$units/gantu-dashboard.service" <<UNIT
[Unit]
Description=Gantu dashboard and tag collector
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
cat > "$units/gantu-backup.service" <<UNIT
[Unit]
Description=Back up local Gantu dashboard SQLite history

[Service]
Type=oneshot
User=$user
WorkingDirectory=$root
ExecStart=$root/dashboard/.venv/bin/python -m dashboard.backup
UMask=0077
UNIT
cat > "$units/gantu-backup.timer" <<'UNIT'
[Unit]
Description=Daily Gantu dashboard backup

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
    cat > "$units/gantu-tunnel.service" <<UNIT
[Unit]
Description=Gantu dashboard named Cloudflare Tunnel
Wants=network-online.target gantu-dashboard.service
After=network-online.target gantu-dashboard.service

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
sudo install -m 644 "$units/"* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gantu-dashboard.service gantu-backup.timer
sudo systemctl restart gantu-dashboard.service
if [ -f "$units/gantu-tunnel.service" ]; then
    sudo systemctl enable --now gantu-tunnel.service
    sudo systemctl restart gantu-tunnel.service
fi
echo 'Gantu is enabled at boot. Run dashboard/.venv/bin/python -m dashboard.check to verify.'
