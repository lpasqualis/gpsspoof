# gpsspoof

A small macOS CLI that spoofs the GPS location of a USB-connected iPhone
using [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).
Pure Python, no GUI. Targets iOS 17 and newer (verified on iOS 26).

```text
$ sudo gpsspoof set seattle
connected: My iPhone (iPhone18,2) iOS 26.4.2
udid:      00008030-0123456789ABCDEF
... scanning for RemoteXPC service (Bonjour, ~3s)...
... found RemoteXPC service in 3.1s
... establishing TCP tunnel...
... tunnel up at fd00:6:1:2:3:4:5:6:54321 (0.4s)
... connecting to RemoteServiceDiscovery...
... opening DVT channel...
... DVT channel ready (1.2s)

  SPOOFING ACTIVE  →  seattle  (47.6062, -122.3321)
  device           →  My iPhone
  pid              →  49823

  press Ctrl-C to clear and exit

^C
... received stop signal after 47.3s, clearing location...
... cleared. real GPS resumed.
```

---

## Contents

- [How it works](#how-it-works)
- [Phone setup](#phone-setup)
- [Install](#install)
- [Quick start](#quick-start)
- [Command reference](#command-reference)
  - [`gpsspoof ui`](#gpsspoof-ui)
  - [`gpsspoof list`](#gpsspoof-list)
  - [`gpsspoof set`](#gpsspoof-set-name)
  - [`gpsspoof clear`](#gpsspoof-clear)
  - [`gpsspoof status`](#gpsspoof-status)
  - [`gpsspoof add`](#gpsspoof-add-name-lat-lon)
  - [`gpsspoof rm`](#gpsspoof-rm-name)
- [Locations file](#locations-file)
- [State file](#state-file)
- [Output, exit codes, and stderr/stdout split](#output-exit-codes-and-stderrstdout-split)
- [Why does it need sudo?](#why-does-it-need-sudo)
- [Skip sudo with tunneld](#skip-sudo-with-tunneld)
- [Troubleshooting](#troubleshooting)
- [Comparison with the official `pymobiledevice3` CLI](#comparison-with-the-official-pymobiledevice3-cli)
- [Multiple iPhones](#multiple-iphones)
- [Limitations](#limitations)
- [Requirements](#requirements)

## How it works

```
usbmux.list_devices()                   ─── find iPhones over USB
        │
        ├─ lockdown query                ─── model + iOS version (no root)
        │
        └─ get_core_device_tunnel_services()
                │                        ─── pause `remoted`,
                │                            Bonjour-scan for CoreDevice
                │                            tunnel service  (NEEDS ROOT)
                │
                └─ start_tunnel_over_core_device(protocol=TCP)
                        │                ─── bring up TCP tunnel
                        │
                        └─ RemoteServiceDiscoveryService
                                │
                                └─ DvtProvider
                                        │
                                        └─ LocationSimulation
                                                .set(lat, lon)
                                                .clear()
```

`LocationSimulation.set` issues the same DVT instrument call Xcode uses
(`simulateLocationWithLatitude:longitude:`). `.clear()` issues
`stopLocationSimulation`, restoring the real GPS feed.

The tool uses **TCP** for the tunnel because iOS 18.2 removed QUIC
support; TCP works for all iOS 17+ versions, so there's no version-gated
branching.

## Phone setup

One-time, on the iPhone:

1. Plug into the Mac with a **data**-capable USB cable (not a
   power-only cable). Tap "Trust This Computer" on the device.
2. Enable Developer Mode:
   `Settings → Privacy & Security → Developer Mode → On`. The phone
   reboots and asks you to confirm Developer Mode after the reboot —
   you have to confirm it again, otherwise the toggle reverts.
3. Make sure the phone is **unlocked** when running `sudo gpsspoof
   set …`. RemoteXPC services are only exposed while unlocked.

The developer disk image (DDI) is bundled with iOS 17+ and mounts
lazily on first developer-tool access. You don't usually need to mount
it manually — but if `gpsspoof set` errors with "no RemoteXPC tunnel
service found", that's the most likely cause.

## Install

```bash
git clone https://github.com/lpasqualis/gpsspoof.git
cd gpsspoof
python3 -m venv .venv
.venv/bin/pip install -e .
```

The editable install puts a `gpsspoof` script at `.venv/bin/gpsspoof`.

To make plain `gpsspoof` and `sudo gpsspoof` both work without
typing the venv path, drop two symlinks:

```bash
# user-writable, on your normal PATH
ln -sf "$PWD/.venv/bin/gpsspoof" /opt/homebrew/bin/gpsspoof

# in macOS sudo's default secure_path, so `sudo gpsspoof` works
sudo ln -sf "$PWD/.venv/bin/gpsspoof" /usr/local/bin/gpsspoof
```

Verify:

```bash
gpsspoof --help
sudo gpsspoof --help
```

> Why two symlinks? `/opt/homebrew/bin` is on your interactive PATH but
> isn't in macOS's compiled-in `secure_path`, so `sudo` won't find a
> binary there. `/usr/local/bin` is in `secure_path` but isn't user
> writable. One symlink in each gives you both ergonomics without
> editing `sudoers`.

### Alternative: `pipx`

If you prefer pipx, `pipx install .` works for the unprivileged
commands but `sudo gpsspoof` still won't resolve unless you also
symlink the pipx entry into `/usr/local/bin` as above.

## Quick start

```bash
gpsspoof ui                       # interactive menu (recommended)

gpsspoof                          # no args ⇒ prints help
gpsspoof list                     # list named locations
gpsspoof set seattle              # one-shot foreground spoof; Ctrl-C clears
gpsspoof status                   # show what's running (from any shell)
gpsspoof clear                    # explicitly clear the device

gpsspoof add airport 47.4502 -122.3088   # add or update a location
gpsspoof rm airport                      # remove one
```

> `ui`, `set`, `clear` need either `sudo` **or** a running tunneld
> daemon (set up once — see [Skip sudo with tunneld](#skip-sudo-with-tunneld)).
> The tool auto-detects tunneld; if it isn't running, prefix the
> command with `sudo`.

## Command reference

### `gpsspoof ui`

Interactive mode. **Needs root or a running tunneld.** Pick a location from a numbered
menu, watch the tunnel come up, see a live elapsed-time counter while
the device is being spoofed, then press any key to clear and pick a
different location. `q` (or empty input) at the menu exits cleanly;
Ctrl-C at any point clears the device and quits the whole UI.

```text
────────────────────────────────────────────────────────────
  gpsspoof  interactive mode
  device:  My iPhone (iPhone18,2) iOS 26.4.2
  udid:    00008030-0123456789ABCDEF
────────────────────────────────────────────────────────────

Select a location:
  [ 1]  bellevue   47.6101, -122.2015
  [ 2]  issaquah   47.5301, -122.0326
  [ 3]  kent       47.3809, -122.2348
  ...
  [12]  vegas      36.1699, -115.1398
  [ q]  quit

> 1
→ engaging: bellevue (47.6101, -122.2015)
... scanning for RemoteXPC service (Bonjour, ~3s)...
... tunnel up at fd00:6:1:2:3:4:5:6:54321 (0.4s)
... DVT channel ready (1.2s)

  ┌─ SPOOFING ACTIVE
  │  bellevue  (47.6101, -122.2015)
  │  My iPhone  [00008030-0123456789ABCDEF]
  │
  └─ press any key to clear and return to menu  (Ctrl-C to quit)
  elapsed: 0:00:42
[user presses space]
  clearing...
  cleared, real GPS resumed

Select a location:
...
```

`ui` is the simplest way to flip between locations rapidly: each
selection re-establishes the tunnel, sets the location, and tears it
down on key press. The tunnel/DVT setup runs once per selection, so
expect the same 5–20 s warm-up each time.

### `gpsspoof list`

Print all named locations from `~/.config/iphone-spoof/locations.json`,
sorted alphabetically. Auto-creates the file on first run with 12
default locations (see [Locations file](#locations-file)).

```text
  bellevue     47.6101,  -122.2015
  issaquah     47.5301,  -122.0326
  kent         47.3809,  -122.2348
  ...
```

### `gpsspoof set NAME`

Start spoofing the connected iPhone's GPS to the named location.
**Needs root or a running tunneld.** The process stays foregrounded until you press
Ctrl-C; on exit (clean, Ctrl-C, or any exception) it sends a clear
command so the real GPS feed resumes immediately.

`NAME` must be a key from `locations.json` (case-sensitive).

While it runs, `~/.config/iphone-spoof/state.json` records the active
session so `gpsspoof status` from any other shell can describe it.

```bash
sudo gpsspoof set seattle
sudo gpsspoof --udid 00008030-0123456789ABCDEF set tacoma
```

### `gpsspoof clear`

Send a clear-location command to the device, restoring real GPS.
**Needs root or a running tunneld.** Useful when a previous `set` was killed without
being able to clean up (e.g. `kill -9`, terminal closed without
Ctrl-C, crash). Safe to run when nothing is spoofed.

If `state.json` records a UDID, `clear` targets that device by default
even if multiple iPhones are connected.

### `gpsspoof status`

Show the currently active spoof session, if any:

```text
spoofing:
  device:   My iPhone [00008030-0123456789ABCDEF]
  location: seattle (47.6062, -122.3321)
  pid:      49823
```

If `state.json` exists but the recorded PID is gone, prints a "stale
state" warning suggesting `sudo gpsspoof clear`. If neither, prints
`no active spoof`.

Runs unprivileged.

### `gpsspoof add NAME LAT LON`

Add or update a location. Latitude must be in `[-90, 90]`, longitude
in `[-180, 180]`. Existing names are silently overwritten:

```bash
gpsspoof add liberty 40.6892 -74.0445
```

Prints `added` or `updated` so you can tell which happened.

### `gpsspoof rm NAME`

Remove a location. Errors if `NAME` isn't present.

### `gpsspoof --udid UDID …`

Disambiguate when multiple iPhones are plugged in. Applies to `set`,
`clear`, and `status`. Without it, those commands refuse to guess and
print all UDIDs.

## Locations file

Path: `~/.config/iphone-spoof/locations.json`

Schema (a single JSON object whose keys are location names):

```json
{
  "<name>": { "lat": <float>, "lon": <float> },
  ...
}
```

The first read auto-creates the file with these defaults:

I-5 corridor, **south → north** (Portland OR up to Bellingham WA):

| name        | place               | lat       | lon         |
|-------------|---------------------|----------:|------------:|
| portland    | Portland, OR        |  45.5152  | -122.6784   |
| vancouver   | Vancouver, WA       |  45.6387  | -122.6615   |
| olympia     | Olympia, WA         |  47.0379  | -122.9007   |
| tacoma      | Tacoma, WA          |  47.2529  | -122.4443   |
| federal-way | Federal Way, WA     |  47.3223  | -122.3126   |
| kent        | Kent, WA            |  47.3809  | -122.2348   |
| renton      | Renton, WA          |  47.4829  | -122.2171   |
| issaquah    | Issaquah, WA        |  47.5301  | -122.0326   |
| seattle     | Seattle, WA         |  47.6062  | -122.3321   |
| bellevue    | Bellevue, WA        |  47.6101  | -122.2015   |
| redmond     | Redmond, WA         |  47.6740  | -122.1215   |
| everett     | Everett, WA         |  47.9790  | -122.2021   |
| marysville  | Marysville, WA      |  48.0517  | -122.1771   |
| bellingham  | Bellingham, WA      |  48.7519  | -122.4787   |

Other US:

| name      | place               | lat       | lon         |
|-----------|---------------------|----------:|------------:|
| vegas     | Las Vegas, NV       |  36.1699  | -115.1398   |
| la        | Los Angeles, CA     |  34.0522  | -118.2437   |
| lax       | LAX airport         |  33.9416  | -118.4085   |
| nyc       | New York, NY        |  40.7128  |  -74.0060   |

You can hand-edit the file (it's just JSON) or use `gpsspoof add` /
`gpsspoof rm`.

> Sudo and `~`: under `sudo`, `~` would normally resolve to `/root`.
> `gpsspoof` reads `SUDO_USER`'s home instead, so the same
> `locations.json` is used whether or not the command is prefixed
> with `sudo`.

## State file

Path: `~/.config/iphone-spoof/state.json`

Created by `gpsspoof set` on success, removed on clean exit. Contents:

```json
{
  "udid": "00008030-0123456789ABCDEF",
  "device_name": "My iPhone",
  "name": "seattle",
  "lat": 47.6062,
  "lon": -122.3321,
  "pid": 49823
}
```

Read-only as far as the tool is concerned — it's there so a separate
`gpsspoof status` invocation (potentially from another shell) can
describe the running session and check that the PID is alive.

If `set` is killed in a way that prevents cleanup (`kill -9`, power
loss, machine sleep), the file persists. `gpsspoof status` detects
this with a `os.kill(pid, 0)` liveness check and shows a stale-state
warning. The device may still hold the simulated fix in that case;
`sudo gpsspoof clear` resets it.

## Output, exit codes, and stderr/stdout split

`gpsspoof` separates progress chatter from results:

- **stdout** — durable output: list rows, status lines, the
  `SPOOFING ACTIVE` block, `cleared 'X'`, `added 'X'`, etc.
- **stderr** — transient stage updates: lines starting with `... `
  (the Bonjour scan, tunnel timing, DVT handshake), the
  auto-creation notice for `locations.json`, and warnings.

Pipe-friendly:

```bash
gpsspoof list 2>/dev/null | awk '{print $1}'
```

Exit codes:

| code | meaning                                              |
|------|------------------------------------------------------|
| 0    | success                                              |
| 1    | most failures (printed to stderr via `sys.exit(msg)`) |
| 2    | argparse usage error                                 |
| 130  | interrupted (`Ctrl-C` during `set` / `clear`)        |

## Why does it need sudo?

iOS 17 redesigned the developer-tools protocol to use RemoteXPC. To
reach those services from a Mac, two privileged things have to happen:

1. **Pause `remoted`.** macOS's own `remoted` daemon would otherwise
   intercept the Bonjour records advertising the on-device CoreDevice
   tunnel service. `pymobiledevice3` sends `SIGSTOP` to it during the
   scan and `SIGCONT` afterwards. Signaling system daemons requires
   root.
2. **Open the TCP tunnel.** The tunnel binds and connects in the
   protected network namespace. (iOS 18.2+ removed QUIC; TCP is the
   only protocol that still works.)

Everything after the tunnel is up — the actual `set` / `clear` DVT
calls — could in principle run unprivileged. There are two ways to
satisfy steps 1 and 2:

- **`sudo gpsspoof …`** — `gpsspoof` does both steps itself. Simple,
  no extra moving parts, but you `sudo` every time. (NOPASSWD sudoers
  rule eliminates the password prompt — see the install section.)
- **Run a `tunneld` daemon as root** — a long-lived process owns
  the privilege, exposes a localhost API, and `gpsspoof` borrows a
  ready-made tunnel from it without ever needing root itself. See
  [Skip sudo with tunneld](#skip-sudo-with-tunneld).

`gpsspoof` auto-detects tunneld at `127.0.0.1:49151` and prefers it
when available, falling back to the in-process path otherwise. You
can install tunneld at any time and the existing commands just start
working without `sudo`.

`list`, `status`, `add`, `rm` need none of this — they only touch
`~/.config/iphone-spoof/` and (for `status`) usbmux info via
usbmuxd's user socket.

The state file and locations file are owned by your user (the tool
explicitly resolves `SUDO_USER`'s home), so a `sudo gpsspoof set …`
run leaves files you can read and edit afterwards without sudo.

## Skip sudo with tunneld

`pymobiledevice3` ships with a `tunneld` daemon that holds the
privileged plumbing (the `remoted` pause + the TCP tunnel) and
exposes a localhost HTTP API (`127.0.0.1:49151`). Once it's running,
unprivileged clients — including `gpsspoof` — can borrow a tunnel
without elevating themselves.

### What you gain

- `gpsspoof set seattle`, `gpsspoof clear`, `gpsspoof ui` all run
  without `sudo`.
- The tunnel is kept warm between calls, so the 5–20 s setup cost
  collapses to ~0 s after the first connect.

### What it costs

- One always-running process (~25 MB resident, idle most of the time).
- Initial setup needs `sudo` once.
- Tunneld occasionally needs a restart after long sleep/wake cycles.

### Install (manual)

You need the path to the `pymobiledevice3` binary inside whatever
environment has it installed (`which pymobiledevice3`, e.g.
`/Users/you/path/to/.venv/bin/pymobiledevice3`).

Write a launchd plist:

```bash
# 1. Find the binary
PMD3=$(which pymobiledevice3)
echo "$PMD3"   # sanity check

# 2. Drop the plist into /Library/LaunchDaemons/
sudo tee /Library/LaunchDaemons/com.gpsspoof.tunneld.plist >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gpsspoof.tunneld</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PMD3</string>
        <string>remote</string>
        <string>tunneld</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/var/log/gpsspoof-tunneld.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/gpsspoof-tunneld.log</string>
</dict>
</plist>
EOF

# 3. Permissions launchd insists on
sudo chown root:wheel /Library/LaunchDaemons/com.gpsspoof.tunneld.plist
sudo chmod 644 /Library/LaunchDaemons/com.gpsspoof.tunneld.plist

# 4. Load it
sudo launchctl bootstrap system /Library/LaunchDaemons/com.gpsspoof.tunneld.plist
```

Verify it's listening:

```bash
nc -z 127.0.0.1 49151 && echo "tunneld is up"
```

Now plain `gpsspoof set seattle` (no `sudo`) should work. The first
stage line will read:

```
... borrowed tunnel from tunneld in 0.0s (no root needed in this process)
```

### Uninstall

```bash
sudo launchctl bootout system/com.gpsspoof.tunneld
sudo rm /Library/LaunchDaemons/com.gpsspoof.tunneld.plist
```

`gpsspoof` automatically falls back to the in-process tunnel after
tunneld goes away, so removing it is non-destructive.

### Troubleshooting tunneld

- **`gpsspoof` still says "needs root"** — tunneld isn't reachable.
  Check `nc -z 127.0.0.1 49151`, then `tail /var/log/gpsspoof-tunneld.log`.
- **tunneld is up but `gpsspoof` falls back to in-process** — tunneld
  hasn't paired the device yet. Plug the phone in, unlock it, wait a
  few seconds for tunneld to discover it.
- **Stops working after wake from sleep** — known: `remoted`
  occasionally lands in a stuck state. Restart tunneld:
  `sudo launchctl kickstart -k system/com.gpsspoof.tunneld`.

## Troubleshooting

### `no iPhone connected over USB`

- Unlock the phone.
- Re-tap "Trust This Computer" if it's been a while.
- Try a different cable. USB-A → Lightning power-only cables are
  common and look identical to data cables. Same for some cheap USB-C
  cables.
- Check `system_profiler SPUSBDataType | grep -A 3 -i iphone` — if
  macOS itself doesn't see the phone, no software fix will help.

### `RemoteXPC tunnel setup needs root on macOS`

Prefix the command with `sudo`. If you installed via the two-symlink
recipe in [Install](#install), `sudo gpsspoof set seattle` works
directly. Otherwise: `sudo $(which gpsspoof) set seattle`.

### `no RemoteXPC tunnel service found`

In rough order of likelihood:

1. Developer Mode is off (`Settings → Privacy & Security → Developer
   Mode`). Toggle on, reboot, **and** confirm again after the reboot.
2. Phone is locked. Unlock and retry.
3. Developer disk image hasn't mounted. Plug in, unlock, open Xcode
   once (or run `pymobiledevice3 mounter auto-mount`), then retry.
4. First scan after device reboot can take longer than the 3 s
   Bonjour timeout — retry the command.

### `sudo: gpsspoof: command not found`

`sudo`'s `secure_path` excludes `/opt/homebrew/bin`. Either install
the second symlink (see [Install](#install)) or run with
`sudo $(which gpsspoof) …`.

### `iOS 18.2+ removed QUIC protocol support`

Shouldn't appear with this tool — we already use TCP. If you're
seeing it, you're probably running the official `pymobiledevice3`
command directly with default options; pass `--protocol tcp`.

### Phone keeps the spoofed location after Ctrl-C

The Ctrl-C path explicitly calls `LocationSimulation.clear()`. If you
killed the process with `kill -9`, force-quit the terminal, or the
machine slept, that cleanup didn't run. Fix:

```bash
sudo gpsspoof clear
```

### Stage line "scanning for RemoteXPC service" hangs > 30 s

Bonjour scan is using a 3 s timeout. If you're stuck *much* longer,
the next step (TCP tunnel) is the culprit. Try:

```bash
sudo /Users/you/path/to/.venv/bin/python -m pymobiledevice3 remote browse
```

If that hangs too, restart `remoted` (kill `remoted` with `sudo
killall -9 remoted` and let launchd restart it) and retry. Often
`remoted` ends up in a bad state after sleep/wake cycles.

### Apple Maps blue dot didn't move

- Confirm `gpsspoof set` actually printed `SPOOFING ACTIVE` (not just
  the early stage lines).
- Open Apple Maps (not Google Maps or third-party apps — many cache
  location for several minutes). The dot should jump within a second.
- If Maps still shows your real position, force-quit and reopen Maps.

## Comparison with the official `pymobiledevice3` CLI

`gpsspoof` is a thin wrapper for one specific workflow. The same
underlying calls are exposed by the upstream CLI; here's the mapping
in case you need to drop down a level:

| `gpsspoof`                       | `pymobiledevice3` equivalent (with tunneld running)                 |
|----------------------------------|--------------------------------------------------------------------|
| `sudo gpsspoof set NAME`         | `pymobiledevice3 developer dvt simulate-location set --tunnel '' -- LAT LON` |
| `sudo gpsspoof clear`            | `pymobiledevice3 developer dvt simulate-location clear --tunnel ''` |
| (built-in tunnel setup)          | `sudo pymobiledevice3 remote tunneld` (separate, persistent)        |
| (no equivalent — it's local)     | `pymobiledevice3 lockdown info`                                     |

The big behavioral differences:

- `gpsspoof set` brings up the tunnel itself per-invocation, so there
  is **no** persistent `tunneld` daemon to manage.
- Locations are looked up by name from a local JSON file; the upstream
  CLI takes raw `lat lon` arguments.
- `gpsspoof` registers signal handlers that explicitly clear on exit.

If `gpsspoof` ever fails in a way that points at upstream tunneling,
the upstream CLI is the lowest-friction fallback for diagnosing.

## Multiple iPhones

Plug in two iPhones and run `gpsspoof set sf`:

```text
multiple iPhones connected; specify --udid:
  00008030-0123456789ABCDEF  My iPhone  (iPhone18,2, iOS 26.4.2)
  00008140-001234567890ABCD  Lab phone (iPhone16,1, iOS 18.4)
```

Pass `--udid` (top-level flag, before the subcommand):

```bash
sudo gpsspoof --udid 00008030-0123456789ABCDEF set seattle
```

Note: a single device often shows up twice in raw `usbmuxd` output —
once over `USB`, once over `Network` (when Wi-Fi sync is on).
`gpsspoof` filters to USB only and dedupes on UDID, so each physical
device appears exactly once.

## Limitations

- **macOS only.** The tunnel setup uses `utun` and `SIGSTOP` of
  `remoted`, both Darwin-specific.
- **One simulated point at a time.** No GPX route playback (the
  underlying `LocationSimulation.play_gpx_file()` exists but isn't
  exposed). Add a `play` subcommand if you need it.
- **No altitude / speed / course.** Apple's DVT API only takes
  lat/lon.
- **Ctrl-C clear is best-effort.** If the device is already
  disconnected when you Ctrl-C, the clear call fails and you'll see
  `warning: clear failed: …`. Reconnect and run `sudo gpsspoof clear`.

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.10+
- `pymobiledevice3 >= 9.0`
- iPhone running iOS 17 or newer with Developer Mode enabled

## License

MIT (see `pyproject.toml`).
