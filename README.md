# Self-host the Gantu dashboard

Run the dashboard on a Raspberry Pi or a laptop. It fetches reports for your Apple Find My
(OpenHaystack) and Hubble tags and shows them together in a device selector. Each tag has its own
history, with a map, timeline and CSV/GeoJSON export. Hubble packets can also show voltage,
temperature and the advert count since power-up.

Start with a tag that already advertises on the network you chose in the
[programming guide](programming.md#network-payloads). The Pi or laptop needs internet access to
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
git clone https://github.com/pvnkmrksk/gantu-tag.git
cd gantu-tag
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
   `sudo systemctl stop gantu-dashboard.service` on an installed Pi.

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
3. In Gantu, open **Devices & connections → Connect Hubble**. Enter the organization ID and
   the API token. The collector discovers devices in that organization, then polls for packets.
   New registrations can take about five minutes to appear. If the token expires, enter a
   replacement for the same organization ID.
4. Select the device and look under **Hubble packets**. The map shows locations of the
   relays that heard the tag. A packet without a usable location can still show its readings.

Hubble's API returns the decrypted payload as base64. For the seven-byte Gantu format the
dashboard decodes supply voltage, signed temperature and the unsigned advert count. Other payloads
are shown as raw packets. The field layout and example are in
[Network payloads](programming.md#network-payloads).

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
systemctl is-enabled gantu-dashboard.service gantu-backup.timer
sudo systemctl restart gantu-dashboard.service
journalctl -u gantu-dashboard.service -n 40 --no-pager
```

When you can interrupt service, reboot the Pi and run `dashboard.check` again. The Pi's LAN
address can change with DHCP. Use its `.local` hostname where available, or the stable HTTPS
address below.

## 4. Use the same HTTPS address after a reboot (optional)

A named Cloudflare Tunnel gives the dashboard a stable hostname and forwards HTTPS to the
loopback server. Use a domain you manage with Cloudflare and install **cloudflared** on your host.
For the setup commands and install packages, see
[Cloudflare's locally managed tunnel guide](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/).

1. Run `cloudflared tunnel login`, then `cloudflared tunnel create gantu-dashboard`. The latter
   gives you a tunnel UUID and the path to its credential JSON. Add a DNS route using your own
   hostname, for example:

   ```sh
   cloudflared tunnel route dns gantu-dashboard tracker.example.org
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

GitHub is for source code. The dashboard's entire `data/` directory and virtual environment
are ignored. The collector doesn't upload its keys, logs, backups or history to GitHub. Map
tiles come from [OpenFreeMap](https://openfreemap.org/), so the browser requests tiles for the
area you're viewing. Only track tags you own, or have permission to follow.
