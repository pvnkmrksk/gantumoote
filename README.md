# Gantumoote

*ಗಂಟುಮೂಟೆ*, a bundle of everything you carry: the self-hosted dashboard for [gantu tags](https://github.com/pvnkmrksk/gantu-tag),
the insect-sized Find My and Hubble trackers. It collects every tag's sightings on your own Raspberry Pi or
laptop and maps them, full screen, with the newest sighting the most vivid. Tap the map to hide the panels.

Run the dashboard on a Raspberry Pi or a laptop. It fetches reports for your Apple Find My
(OpenHaystack) and Hubble tags and shows them together in a device selector. Each tag has its own
history, with a map, timeline and CSV/GeoJSON export. Hubble packets can also show voltage,
temperature and the advert count since power-up.

Start with a tag that already advertises on the network you chose in the
[programming guide](https://github.com/pvnkmrksk/gantu-tag/blob/main/docs/programming.md#network-payloads). The Pi or laptop needs internet access to
fetch reports. The tags can be anywhere a relay hears them.

## 1. Start the dashboard

On a Pi, use **64-bit Raspberry Pi OS**. On a laptop, use macOS or Linux. Use Python **3.11–3.13**
and Git. On a fresh Raspberry Pi OS or Debian install, run:

```sh
sudo apt update
sudo apt install git python3 python3-venv
```

On macOS, if you need Python, `brew install python@3.13` installs it as `python3.13`. On either
host, run the following in a terminal (use `python3.13` on the second line if needed):

```sh
git clone https://github.com/pvnkmrksk/gantumoote.git
cd gantumoote
python3 -m venv dashboard/.venv
dashboard/.venv/bin/python -m pip install -r dashboard/requirements.txt
dashboard/.venv/bin/python -m dashboard.server
```

Keep that terminal open on a laptop. In a second terminal in the checkout, read your dashboard
access code:

```sh
cat dashboard/data/secrets/access-code.txt
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766) and enter the code. The first run starts with
an empty device list. If the dashboard runs on a headless Pi, forward its local port from your
laptop over SSH (replace the login below with your Pi's):

```sh
ssh -N -L 8766:127.0.0.1:8766 your-user@your-pi.local
```

Then open the same local address in your laptop browser. Use SSH to read the access code on your
Pi. The dashboard listens on loopback. Keep the access code private, since it grants access to
every device, export and management action in this installation.

Each browser signs in once and remembers it. **Share view** copies a link to the current map view
without the access code, so whoever opens it has to sign in. Links from versions before 20 Nov 2026
carried the code in the address (`#access=`). If one of those was ever shared, change the code:
stop the dashboard, delete `dashboard/data/secrets/access-code.txt` and start it again. It makes a
new code, and every browser has to sign in again with it.

## 2. Add devices

### Apple Find My

Use the **Private key** line from the `.keys` file from `tools/new_findmy_tag.py`, or from the
key record downloaded from the build guide's Find My form. This is the 28-byte private key in
standard base64 matching the advert you burned into the tag.

1. Install Git and 7-Zip. On Raspberry Pi OS or Debian use `sudo apt install 7zip` (on older releases
   use `sudo apt install p7zip-full`). On macOS use `brew install sevenzip`.
2. Run the following on the host that will collect the reports, under the same user that runs
   the dashboard. Stop a running dashboard first with Ctrl-C or
   `sudo systemctl stop gantumoote.service` on an installed Pi.

   ```sh
   dashboard/.venv/bin/python -m pip install -r dashboard/requirements-apple.txt
   dashboard/.venv/bin/python -m dashboard.setup_apple
   dashboard/.venv/bin/python -m dashboard.apple login
   ```

   Setup fetches the pinned [FindMy.py source](https://github.com/parawanderer/FindMy.py/commit/7969003ca785658369b650f75d9e7ca519566bf6)
   and the [CoreADI emulator](https://github.com/sacca97/os-findmy-e/tree/d4d2a832baf47d1cf43d989879950ac2aec45f47).
   It downloads Apple's installer (about 290 MB), verifies both checksums and extracts the
   required library into the local data directory. It does not install iTunes. The emulator
   runs on the Pi's ARM64 CPU as well as a laptop.
3. Enter your Apple account email and regular password in the terminal. Use an account that
   has been signed in on an Apple device, with a trusted device available for the verification
   code. Sign-in stores a password-free session and its matching machine identity on this host.
   This collector uses the unofficial OpenHaystack/FindMy.py flow, so Apple can expire the session
   or change the sign-in flow. The [upstream Apple sign-in guide](https://github.com/malmeloo/FindMy.py/blob/main/docs/getstarted/01-account.md)
   explains account requirements.
4. Start the dashboard again. Open **Devices & connections → Add OpenHaystack tag**. Enter a
   name and paste the tag's private key. Add one entry for each tag. The saved tag keys use the
   same Apple session, and the dashboard fetches them in a batch about once a minute.

If you see **Sign-in needed**, stop the dashboard, rerun `dashboard/.venv/bin/python -m dashboard.apple login`
as the same user, then start the dashboard again. An old sighting with a recent successful
check can simply mean no relay has reported a new position.

### Hubble

1. Register the tag in [Hubble Dashboard](https://dash.hubble.com). For the Gantu template,
   choose **AES-128-EAX**, **DEVICE_UPTIME**, and period exponent **15**. Burn the matching
   config and confirm a packet for that device in Hubble Dashboard.
2. In Hubble's **Developer Tools → API Tokens**, get your organization ID and create a token
   with **read-devices** and **read-packets**. See the [Hubble Platform API](https://hubble.com/docs/api-specification/hubble-platform-api).
3. In Gantumoote, open **Devices & connections → Connect Hubble**. Enter the organization ID and
   the API token. The collector discovers devices in that organization, then polls for packets.
   New registrations can take about five minutes to appear. If the token expires, enter a
   replacement for the same organization ID.
4. Select the device and look under **Hubble packets**. The map shows locations of the
   relays that heard the tag. A packet without a usable location can still show its readings.

Hubble's API returns the decrypted payload as base64. For the seven-byte Gantu format the
dashboard decodes supply voltage, signed temperature and the unsigned advert count. Other payloads
are shown as raw packets. The field layout and example are in
[Network payloads](https://github.com/pvnkmrksk/gantu-tag/blob/main/docs/programming.md#network-payloads).

**Save key** on a Hubble device stores its AES-128 key for your records (32 hex digits or standard
base64). The organization token is what the collector uses to fetch cloud reports.

## 3. Start automatically on a Pi

Stop the foreground server with Ctrl-C if it is still running, then run this as your regular user
from the repository root:

```sh
dashboard/pi/install.sh
dashboard/.venv/bin/python -m dashboard.check
```

The installer enables the dashboard and the daily backup timer through systemd. The dashboard
restarts after a crash and starts when power returns and the Pi boots, without a desktop login.
On a laptop, the foreground server runs while the laptop is awake. The same systemd installer
works on a Linux laptop that uses systemd.

```sh
systemctl is-enabled gantumoote.service gantumoote-backup.timer
sudo systemctl restart gantumoote.service
journalctl -u gantumoote.service -n 40 --no-pager
```

When you can interrupt service, reboot the Pi and run `dashboard.check` again. The Pi's LAN
address can change with DHCP. Use its `.local` hostname where available, or the stable HTTPS
address below.

## 4. Use the same HTTPS address after a reboot (optional)

A named Cloudflare Tunnel gives the dashboard a stable hostname and forwards HTTPS to the
loopback server. Use a domain you manage with Cloudflare and install **cloudflared** on your host.
For the setup commands and install packages, see
[Cloudflare's locally managed tunnel guide](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/).

1. Run `cloudflared tunnel login`, then `cloudflared tunnel create gantumoote`. The latter
   gives you a tunnel UUID and the path to its credential JSON. Add a DNS route using your own
   hostname, for example:

   ```sh
   cloudflared tunnel route dns gantumoote tracker.example.org
   ```

2. Create `dashboard/data/cloudflared.yml`. Replace the UUID, credential path and hostname in
   this example. The path must be absolute and readable by the user running the service.
   Keep the credential JSON private on the host.

   ```yaml
   tunnel: YOUR-TUNNEL-UUID
   credentials-file: /absolute/path/to/YOUR-TUNNEL-UUID.json
   ingress:
     - hostname: tracker.example.org
       service: http://127.0.0.1:8766
     - service: http_status:404
   ```

   Set your dashboard's public URL on the host for checks and push notifications:

   ```sh
   printf '%s\n' 'https://tracker.example.org' > dashboard/data/public-url.txt
   ```

3. On the Pi, run `dashboard/pi/install.sh` again to enable the tunnel service as well. On a
   laptop running the dashboard in the foreground, run this in another terminal:

   ```sh
   cloudflared tunnel --config dashboard/data/cloudflared.yml run
   ```

   Open your HTTPS address, enter the same dashboard access code and run
   `dashboard/.venv/bin/python -m dashboard.check --public`. Keep one active host per tunnel
   so every browser request uses the same local history. Use HTTPS for remote browser
   notifications. On iOS, add the dashboard to the Home Screen, then enable notifications
   from the installed app.

**Changing the hostname later** (for example when a domain lapses) moves no data. Add the new domain to
Cloudflare, run `cloudflared tunnel route dns gantumoote new.example.org`, put the new hostname in
`dashboard/data/cloudflared.yml` and `dashboard/data/public-url.txt`, and restart the tunnel and the
dashboard. Browsers sign in once on the new address, and notifications have to be enabled again on each
device, because both belong to the old address.

## 5. Encrypted cloud backup (optional)

Once a day the host can push an encrypted copy of `dashboard/data/` (history, registry, device keys, the
Apple session, the Hubble token and the access code) to a private Git repository. It's encrypted with
[age](https://age-encryption.org) to a key that you keep elsewhere, so the repository only ever holds
ciphertext. Local snapshots and what `setup_apple` downloads again (FindMy.py, the emulator, Apple's
CoreADI library) are left out. A run commits only when something changed.

1. On a computer you keep (not the host), make the key and keep the identity file in your password
   manager. Without it the backup can't be read:

   ```sh
   age-keygen -o gantumoote-backup-identity.txt   # prints the public key, age1...
   ```

2. Create a private repository, for example `you/gantumoote-backup`. On the host, install age, make a
   deploy key and pin GitHub's SSH host keys (compare the fingerprints with
   [GitHub's published ones](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)):

   ```sh
   sudo apt install age
   ssh-keygen -t ed25519 -N '' -C gantumoote-backup -f dashboard/data/secrets/cloud-backup-key
   ssh-keyscan github.com > dashboard/data/secrets/github-known-hosts
   ssh-keygen -lf dashboard/data/secrets/github-known-hosts
   ```

   Add `cloud-backup-key.pub` to the repository under **Settings → Deploy keys**, with write access.
   That key can reach only this one repository.

3. Point the host at the repository and your public key, push the first backup, and enable the daily
   timer:

   ```sh
   printf '%s\n' '{"repo": "git@github.com:you/gantumoote-backup.git", "recipient": "age1..."}' > dashboard/data/cloud-backup.json
   dashboard/.venv/bin/python -m dashboard.cloud_backup
   dashboard/pi/install.sh
   ```

**Restore** on a new host: install Gantumoote, then

```sh
git clone git@github.com:you/gantumoote-backup.git
mkdir -p dashboard/data
age -d -i gantumoote-backup-identity.txt gantumoote-backup/gantumoote-data.tar.gz.age | tar -xz -C dashboard/data
dashboard/.venv/bin/python -m dashboard.setup_apple
```

Fix the absolute paths in `cloudflared.yml` if the checkout moved, and start only one host. Each backup
adds a few hundred KiB to the repository's history; squash it if it ever grows large.

## Flash history and Pi BLE

Sign in with the normal dashboard access code and open **Devices & connections → Flash history & Pi BLE**.
The table shows confirmed OTP records, prepared tag-specific files and the last advertisement
heard locally by the Pi. Download a prepared config for the physical unit you are about to program.
Each config uses its own tag identity. Keep both the config and its key private.

Start the receiver on Raspberry Pi OS from the checkout:

```sh
sudo apt install bluez python3-cryptography python3-dbus python3-gi python3-matplotlib
dashboard/pi/install-ble.sh
systemctl status gantumoote-ble --no-pager
sudo journalctl -u gantumoote-ble -n 25 --no-pager
/usr/bin/python3 -m dashboard.ble capture ~/blelog/burn.btsnoop
```

Stream directly in a Pi terminal:

```sh
dashboard/pi/gantu-ble -l
dashboard/pi/gantu-ble -d h03 h04 --record
```

The terminal plots the rolling interval between received adverts, RSSI and temperature side by side,
with each tag’s latest raw advert below. The interval is measured per tag and network. A missed advert
can extend the observed interval. The screen refreshes once per second and `--window` sets the plot span.
`--record` saves the private TSV and a sibling PNG in `~/blelog` on the Pi. The PNG appears when the logger starts,
refreshes every 30 seconds when there are new samples, and includes the final samples after Ctrl-C. It shows
packet arrivals and counts, reception gaps with a five-gap rolling median, and raw RSSI and temperature
points with centered weighted averages over up to nine readings. The curves break across long reception gaps. Selected tags that stay silent get an empty row. The terminal prints one line
per advert when piped or run with `--plain`. Link `dashboard/pi/gantu-ble` into `~/.local/bin/` to use `gantu-ble`
from any directory.

Regenerate a private PNG without starting Bluetooth discovery. Use `latest` for the newest recording by
start time, or pass the exact TSV path printed by the logger. Quoting the tilde lets the Pi expand it.

```sh
gantu-ble --plot latest
gantu-ble --plot '~/blelog/recording.tsv'
```

To run the same command from a Mac, install the SSH launcher from this checkout:

```sh
mkdir -p ~/.local/bin ~/.config/gantu-ble
install -m 0755 dashboard/pi/gantu-ble-ssh ~/.local/bin/gantu-ble
```

Set up the `gantu-bench` host in `~/.config/gantu-ble/ssh_config` with the Pi's address, account and
your existing SSH identity and verified host key. The Pi needs its own `~/.local/bin/gantu-ble` link.

```sshconfig
Host gantu-bench
    HostName 192.168.1.50
    User pi
    IdentityFile ~/.ssh/pi_ed25519
    UserKnownHostsFile ~/.ssh/pi_known_hosts
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

On the Mac, `gantu-ble --plot latest` downloads just the generated PNG to `~/Downloads/Gantu BLE`,
prints its local path, and opens it in your default image viewer when run interactively.

To keep private packet-by-packet archives of all locally keyed tags, install the recorder as the Pi user:

```sh
dashboard/pi/install-ble-recording.sh
systemctl status gantumoote-ble-recording --no-pager
```

The service starts at boot, rotates to a new private `known-*.tsv` every 24 hours while discovery keeps running,
and restarts after a failure. The monitor and recorder have a 45-second watchdog for a stalled scan loop.
The watchdog measures the scan loop so solar tags can go silent overnight.
Only adverts matched by the local tag keys reach the recorder. At approximately 120 bytes per received advert,
four tags received every 3 s around the clock use about 14 MB per day before compression.
The service writes TSVs continuously and renders plots on demand. Existing archives stay on the Pi until removed.

In the signed-in dashboard, open **Flash history & Pi BLE → View daily BLE and Find My plots**.
Pick a calendar date in the Pi's local time and optionally enter tag names. The chart combines minute
reception history, exact gaps where packet TSVs exist, and saved Find My observation timestamps. Use the
PNG link to download it. The view and image use the same sign-in as Flash history.

Plot the same day from the Pi or use the Mac SSH launcher to download the PNG.

```sh
gantu-ble --plot-day 2026-11-23 --tags h03 h04 h05 h06
```

Use the same time bins to compare the share of BLE-active windows with Find My reports. An Apple report is
an end-to-end observation, so that share measures reporting coverage for the run.

For a phone with an ambient light sensor exposed to Phyphox, open **Light**, enable remote access,
and press Start. Use the URL shown by the app while the Pi and phone are on the same local network:

```sh
dashboard/pi/gantu-ble -d h03 h04 --record --phyphox http://192.168.1.42:8080
```

The terminal adds a lux plot on the same time window. Recording creates a sibling `.lux.tsv` beside the
BLE TSV with the same absolute timestamp format. The PNG includes a time-aligned light panel when the lux
log has samples. The Pi uses the experiment’s clock references to
timestamp lux samples, so network polling delays do not shift their recorded times. Use automatic
date and time on both devices. Phyphox’s built-in Light experiment requires Android hardware that
exposes an ambient light sensor. [Light experiment](https://phyphox.org/experiment/light/).
[Phyphox time reference](https://phyphox.org/docs/remote-interface/#time).

The BLE monitor uses BlueZ on `hci0`. It matches Find My manufacturer data and authenticates Hubble v2
advertisements using the matching `.keys` and `.hubble.key` files in `dashboard/data/secrets/tag-keys/`. It saves only registered tags and updates the dashboard every
10 seconds. Download a CSV of the last 24 hours of received advertisements, grouped by tag and UTC minute. Hubble network
reports still appear in the main dashboard. Old raw captures stay in the Pi's private `~/blelog`
directory. The capture command reports their actual first and last packet timestamps.

Archive each new OTP burn while the programmer's output is still available. Keep the matching prepared
config and key under `dashboard/data/secrets/tag-keys/` with mode 600. Copy the tool’s output to the Pi
over SSH and run this as the dashboard user with the actual board, timestamp and filenames:

```sh
tag=Hagalu-004
dashboard/.venv/bin/python -m dashboard.flash "$tag" S6L findmy '2026-11-22T18:00:00+01:00' \
  --config ~/programmed.cfg --source hagalu_004.findmy-temp-tx4.cfg --temperature
sudo chattr +i "dashboard/data/flash-history/$tag.json" \
  "dashboard/data/secrets/flash-artifacts/$tag/programmed.cfg"
```

On an ext4 Pi, set `sudo chattr +a dashboard/data/flash-history` after the first record is saved.
That permits new serials to be added and protects the history directory against deletion or replacement.
The command accepts an identical retry and rejects a different record for the same serial.
The existing age-encrypted cloud backup includes these records and files. After a restore, reapply the
file flags and the directory flag on ext4.

## History, keys and backups

| Item | Where it lives |
|---|---|
| Device registry and original Hubble packets | `dashboard/data/registry.sqlite3` |
| Location history | `dashboard/data/devices/` |
| Access code, Apple session and identity, provider and device keys | `dashboard/data/secrets/` |
| Database snapshots | `dashboard/data/backups/` |
| Runtime logs | The terminal on a laptop; the system journal on a Pi |

**Devices & connections → Delete** hides a device and stops collecting for it. It keeps its
history and keys on your host for recovery. Hubble discoveries skip deleted devices while
the organization connection continues to collect for the others. **Pause collection** on an
OpenHaystack tag keeps it visible and lets you resume later. **Rename** changes an OpenHaystack
tag's name and keeps its history; exports use the new name. A Hubble device takes its name from
the Hubble dashboard, so rename it there and the new name appears at the next sync.

The Pi's daily timer keeps the latest 14 SQLite snapshots per database. To make a snapshot on
a laptop, run `dashboard/.venv/bin/python -m dashboard.backup`. For recovery after storage
failure, keep a separate private copy of the snapshots and matching `secrets/` directory.
To move hosts, stop the old dashboard and tunnel, then transfer your own data securely to
the new host. Install the dependencies there and start only that host.

This repository is for source code. The dashboard's entire `data/` directory and virtual environment
are ignored. Apart from the optional encrypted backup above, the collector doesn't upload its keys, logs,
backups or history anywhere. Map
tiles come from [OpenFreeMap](https://openfreemap.org/), so the browser requests tiles for the
area you're viewing. Only track tags you own, or have permission to follow.
