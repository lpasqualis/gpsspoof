# gpsspoof

A small macOS CLI that spoofs the GPS location of a USB-connected iPhone
using [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).
Targets iOS 17 and newer (verified on iOS 26). Pure Python, no GUI.

## What it does

```text
gpsspoof list                     # show known locations
sudo gpsspoof set seattle         # start spoofing (stays in foreground; Ctrl-C to stop)
sudo gpsspoof clear               # clear any active spoof on the phone
gpsspoof status                   # show current state
gpsspoof add airport 47.4502 -122.3088
gpsspoof rm airport
gpsspoof                          # no args == print help
```

`set` keeps the process alive until you press Ctrl-C. On exit (clean,
Ctrl-C, or exception) it issues a clear-location command so the real
GPS resumes immediately.

## Why sudo?

iOS 17+ moved the developer (DVT) services behind RemoteXPC. To talk
to them, the tool has to:

1. Pause the macOS `remoted` daemon so a Bonjour scan can find the
   on-device tunnel service, and
2. Bring up a TCP tunnel to the device.

Both steps need root on macOS. `list`, `status`, `add`, and `rm` only
read or write the local config file and run unprivileged. Only `set`
and `clear` need `sudo`.

> Note: iOS 18.2+ removed QUIC tunnel support; the tool uses the TCP
> tunnel which works on all iOS 17+ versions.

## Phone setup (one-time)

1. Plug the iPhone into the Mac with a data-capable USB cable and tap
   "Trust This Computer" on the device.
2. Enable Developer Mode on the phone:
   `Settings → Privacy & Security → Developer Mode → On`. The phone
   reboots and asks you to confirm Developer Mode after the reboot.
3. Make sure the phone is unlocked when running `sudo gpsspoof set …`.

## Install

From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

That gives you `.venv/bin/gpsspoof`. To make plain `gpsspoof` and
`sudo gpsspoof` both work without typing the venv path, drop two
symlinks:

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

## Default locations

The first `gpsspoof list` (or any other read of the config) creates
`~/.config/iphone-spoof/locations.json` with these entries. Edit the
file directly or use `gpsspoof add` / `gpsspoof rm` to change them.

| name      | place               | lat       | lon         |
|-----------|---------------------|----------:|------------:|
| seattle   | Seattle, WA         |  47.6062  | -122.3321   |
| renton    | Renton, WA          |  47.4829  | -122.2171   |
| kent      | Kent, WA            |  47.3809  | -122.2348   |
| issaquah  | Issaquah, WA        |  47.5301  | -122.0326   |
| bellevue  | Bellevue, WA        |  47.6101  | -122.2015   |
| redmond   | Redmond, WA         |  47.6740  | -122.1215   |
| olympia   | Olympia, WA         |  47.0379  | -122.9007   |
| tacoma    | Tacoma, WA          |  47.2529  | -122.4443   |
| vegas     | Las Vegas, NV       |  36.1699  | -115.1398   |
| portland  | Portland, OR        |  45.5152  | -122.6784   |
| nyc       | New York, NY        |  40.7128  |  -74.0060   |
| la        | Los Angeles, CA     |  34.0522  | -118.2437   |

## Sample session

```text
$ sudo gpsspoof set seattle
connected: My iPhone (iPhone18,2) iOS 26.4.2
udid:      00008030-0123456789ABCDEF
spoofing to seattle (47.6062, -122.3321)
ctrl-c to stop
^C
clearing spoofed location...
```

Open Apple Maps on the phone — the blue dot reacts immediately. Some
third-party apps cache location for a while; Maps is the cleanest way
to confirm.

## Multiple iPhones

If more than one iPhone is plugged in, `set` and `clear` refuse to
guess and print all UDIDs. Pick one with `--udid`:

```bash
sudo gpsspoof --udid 00008030-0123456789ABCDEF set seattle
```

## How it works

1. `pymobiledevice3.usbmux.list_devices()` enumerates USB-connected
   devices; lockdown reports model + iOS version.
2. `get_core_device_tunnel_services(udid=…)` finds the on-device
   RemoteXPC service via Bonjour (after pausing `remoted`).
3. `start_tunnel_over_core_device(service, protocol=TCP)` brings up
   the tunnel and returns a `(host, port)`.
4. `RemoteServiceDiscoveryService((host, port))` connects to the RSD,
   and `DvtProvider(rsd)` opens the DVT channel.
5. `LocationSimulation(dvt).set(lat, lon)` issues the same DVT message
   Xcode uses (`simulateLocationWithLatitude:longitude:`); `clear()`
   sends `stopLocationSimulation`.

While `set` is running, `~/.config/iphone-spoof/state.json` records
the active session so `gpsspoof status` can describe what's in flight.
The state file is removed on clean exit.

## Troubleshooting

- **`no iPhone connected over USB`** — unlock the phone, re-tap Trust,
  try a different cable (must be data-capable, not power-only).
- **`RemoteXPC tunnel setup needs root on macOS`** — prefix the
  command with `sudo`.
- **`no RemoteXPC tunnel service found`** — Developer Mode probably
  isn't on, or the phone is locked, or the developer disk image
  isn't mounted yet (it ships with iOS 17+ but is mounted lazily).
  Unlock the device and retry; the first run after reboot can take
  a few extra seconds.
- **`sudo: gpsspoof: command not found`** — sudo's `secure_path` does
  not include `/opt/homebrew/bin`. Add the second symlink shown in
  [Install](#install), or use `sudo $(which gpsspoof) set seattle`.
- **`status` shows "stale state"** — a previous `set` was killed with
  `kill -9` or crashed before clearing. The phone may still hold the
  fix. Run `sudo gpsspoof clear` to reset it.

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.10+
- `pymobiledevice3 >= 9.0`
- iPhone running iOS 17 or newer with Developer Mode enabled
