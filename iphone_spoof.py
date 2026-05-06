"""gpsspoof: spoof GPS on a USB-connected iPhone (iOS 17+) via pymobiledevice3.

CLI entry point: `gpsspoof` (defined in pyproject.toml). Targets pymobiledevice3 9.x.

Architecture
------------
The chain from the command line to "blue dot moves in Apple Maps" is::

    usbmux.list_devices()                     # find iPhones over USB
        │
        ├─ lockdown.create_using_usbmux()     # query model + iOS version (no root)
        │
        └─ get_core_device_tunnel_services()  # pause `remoted`, Bonjour-scan for
            │                                   the CoreDevice tunnel service
            │                                   (NEEDS ROOT on macOS)
            │
            └─ start_tunnel_over_core_device(protocol=TCP)
                │                              # bring up TCP tunnel; iOS 18.2+
                │                              # removed QUIC, TCP works on all
                │                              # iOS 17+ versions
                │
                └─ RemoteServiceDiscoveryService((host, port))
                    │
                    └─ DvtProvider(rsd)        # DVT channel for instruments
                        │
                        └─ LocationSimulation(dvt)
                                │
                                ├─ .set(lat, lon)   # simulateLocationWithLat...
                                └─ .clear()         # stopLocationSimulation

Two distinct privilege boundaries:

* `list`, `status`, `add`, `rm` only read/write the local JSON config and
  query usbmuxd. They run unprivileged.
* `set` and `clear` need root because the tunnel setup pauses `remoted`
  and creates a TCP tunnel to the device's RemoteXPC services.

The active spoof session is mirrored to `~/.config/iphone-spoof/state.json`
purely so `gpsspoof status` can describe what's running. The state file is
removed on clean exit; if the `set` process is killed with `kill -9`, the
file goes stale and the device may keep the simulated fix until the next
`gpsspoof clear`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import termios
import time
import tty
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional


def _progress(msg: str) -> None:
    """Print a stage line to stderr so it's visually distinct from results."""
    print(f"... {msg}", file=sys.stderr, flush=True)


TUNNELD_HOST = "127.0.0.1"
TUNNELD_PORT = 49151


def _is_tunneld_running() -> bool:
    """Return True if something is listening on the tunneld port.

    This is a fast localhost probe (~ms). False positives only happen if
    a different process is squatting on the port; the actual HTTP exchange
    inside `_try_tunneld` would then fail and we'd fall back cleanly.
    """
    import socket
    try:
        with socket.create_connection((TUNNELD_HOST, TUNNELD_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _check_privileged_or_tunneld(action: str) -> None:
    """Bail out early when the action can't possibly succeed.

    `set` / `clear` / `ui` need either root (to build the tunnel
    in-process) or a running tunneld daemon (to borrow one). If neither
    is available, fail fast with a helpful message instead of letting
    the device-side code fail mid-flow.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return  # root: we can build the tunnel ourselves
    if _is_tunneld_running():
        return  # tunneld will broker the tunnel for us
    sys.exit(
        f"`gpsspoof {action}` needs either:\n"
        f"  - root: re-run as `sudo gpsspoof {action}`, or\n"
        f"  - a running tunneld daemon (see README: 'Skip sudo with tunneld')."
    )


# ANSI sequences for the interactive UI. Skipped automatically when stdout
# is not a tty (e.g. piped output) so logs stay clean.
_USE_ANSI = sys.stdout.isatty()
_CSI = "\x1b["
ANSI = {
    "reset":  _CSI + "0m" if _USE_ANSI else "",
    "bold":   _CSI + "1m" if _USE_ANSI else "",
    "dim":    _CSI + "2m" if _USE_ANSI else "",
    "green":  _CSI + "32m" if _USE_ANSI else "",
    "yellow": _CSI + "33m" if _USE_ANSI else "",
    "cyan":   _CSI + "36m" if _USE_ANSI else "",
    "clr_line": _CSI + "2K\r" if _USE_ANSI else "\r",
}


DEFAULT_LOCATIONS = {
    # I-5 corridor, Portland OR → Bellingham WA (south to north)
    "portland":    {"lat": 45.5152, "lon": -122.6784},
    "vancouver":   {"lat": 45.6387, "lon": -122.6615},  # Vancouver, WA
    "olympia":     {"lat": 47.0379, "lon": -122.9007},
    "tacoma":      {"lat": 47.2529, "lon": -122.4443},
    "federal-way": {"lat": 47.3223, "lon": -122.3126},
    "kent":        {"lat": 47.3809, "lon": -122.2348},
    "renton":      {"lat": 47.4829, "lon": -122.2171},
    "issaquah":    {"lat": 47.5301, "lon": -122.0326},
    "seattle":     {"lat": 47.6062, "lon": -122.3321},
    "bellevue":    {"lat": 47.6101, "lon": -122.2015},
    "redmond":     {"lat": 47.6740, "lon": -122.1215},
    "everett":     {"lat": 47.9790, "lon": -122.2021},
    "marysville":  {"lat": 48.0517, "lon": -122.1771},
    "bellingham":  {"lat": 48.7519, "lon": -122.4787},
    # Other US
    "vegas":       {"lat": 36.1699, "lon": -115.1398},
    "la":          {"lat": 34.0522, "lon": -118.2437},
    "lax":         {"lat": 33.9416, "lon": -118.4085},
    "nyc":         {"lat": 40.7128, "lon":  -74.0060},
}


def get_config_dir() -> Path:
    """Return ~/.config/iphone-spoof, resolving to the invoking user under sudo.

    Without this fallback, `sudo gpsspoof set …` would resolve `~` to
    `/root/.config/...` and read a different (likely empty) locations file
    than `gpsspoof list` does.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        import pwd
        home = Path(pwd.getpwnam(sudo_user).pw_dir)
    else:
        home = Path(os.path.expanduser("~"))
    return home / ".config" / "iphone-spoof"


def locations_path() -> Path:
    return get_config_dir() / "locations.json"


def state_path() -> Path:
    return get_config_dir() / "state.json"


def load_locations() -> dict:
    path = locations_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_LOCATIONS, indent=2) + "\n")
        print(f"created default locations file at {path}", file=sys.stderr)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {path}: {e}")
    if not isinstance(data, dict):
        sys.exit(f"{path} must contain a JSON object")
    for name, loc in data.items():
        if not isinstance(loc, dict) or "lat" not in loc or "lon" not in loc:
            sys.exit(f"location '{name}' must be an object with 'lat' and 'lon'")
    return data


def write_state(state: Optional[dict]) -> None:
    path = state_path()
    if state is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def read_state() -> Optional[dict]:
    path = state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


async def list_iphones() -> list[dict]:
    """Return one entry per USB-connected iPhone (deduped on UDID).

    usbmuxd reports a device once per active connection type — a single
    iPhone can show up twice (USB + Network) when Wi-Fi sync is enabled.
    We restrict to USB and dedupe on serial so each physical device
    appears exactly once.
    """
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices

    seen: set[str] = set()
    out: list[dict] = []
    for d in await list_devices():
        if getattr(d, "connection_type", None) != "USB":
            continue
        if d.serial in seen:
            continue
        seen.add(d.serial)
        try:
            ld = await create_using_usbmux(serial=d.serial, connection_type="USB")
        except Exception as e:
            print(f"warning: could not query {d.serial}: {e}", file=sys.stderr)
            continue
        product = ld.product_type or ""
        if "iPhone" not in product:
            continue
        try:
            device_name = await ld.get_value(key="DeviceName")
        except Exception:
            device_name = None
        out.append({
            "udid": ld.udid,
            "product_type": product,
            "product_version": ld.product_version,
            "device_name": device_name or product,
        })
    return out


async def select_iphone(udid: Optional[str]) -> dict:
    iphones = await list_iphones()
    if not iphones:
        sys.exit("no iPhone connected over USB (is the device unlocked and trusted?)")
    if udid:
        match = [d for d in iphones if d["udid"] == udid]
        if not match:
            sys.exit(f"no connected iPhone with UDID {udid}")
        return match[0]
    if len(iphones) > 1:
        msg = ["multiple iPhones connected; specify --udid:"]
        for d in iphones:
            msg.append(
                f"  {d['udid']}  {d['device_name']}  "
                f"({d['product_type']}, iOS {d['product_version']})"
            )
        sys.exit("\n".join(msg))
    return iphones[0]


async def _try_tunneld(udid: str):
    """Return a connected RSD from a running tunneld, or None if unavailable.

    Reasons we return None (and the caller falls back to the in-process
    tunnel):
      * tunneld isn't running at all — `TunneldConnectionError`,
      * tunneld is running but doesn't have this UDID paired,
      * the cached tunnel info exists but the RSD connect itself fails.

    The caller is responsible for `await rsd.close()` on the returned
    RSD when done.
    """
    if not _is_tunneld_running():
        return None
    try:
        from pymobiledevice3.tunneld.api import (
            TunneldConnectionError,
            get_tunneld_device_by_udid,
        )
    except ImportError:
        return None

    _progress(f"checking tunneld at {TUNNELD_HOST}:{TUNNELD_PORT}...")
    t = time.monotonic()
    try:
        rsd = await get_tunneld_device_by_udid(udid)
    except TunneldConnectionError:
        _progress("tunneld unreachable; will try in-process tunnel")
        return None
    if rsd is None:
        _progress("tunneld is running but doesn't see this UDID; will try in-process tunnel")
        return None
    _progress(f"borrowed tunnel from tunneld in {time.monotonic() - t:.1f}s "
              f"(no root needed in this process)")
    return rsd


@asynccontextmanager
async def _open_rsd_in_process(udid: str):
    """Build a tunnel in this process and yield a connected RSD.

    iOS 17+ exposes developer services over RemoteXPC instead of plain
    lockdown. Reaching them requires three privileged steps on macOS:

    1. SIGSTOP `remoted` so it doesn't intercept the Bonjour responses
       advertising the on-device CoreDevice tunnel service.
    2. Open a TCP tunnel to that service. (iOS 18.2+ dropped QUIC; TCP
       works for all iOS 17+ versions and is the default here.)
    3. Connect to the resulting (host, port) via the RSD client.

    Steps 1 and 2 each error out as `AccessDeniedError` / `EPERM` when
    run without root; both are caught and re-routed to `_need_root()`.
    """
    from pymobiledevice3.exceptions import (
        AccessDeniedError,
        NoDeviceConnectedError,
    )
    from pymobiledevice3.remote.common import TunnelProtocol
    from pymobiledevice3.remote.remote_service_discovery import (
        RemoteServiceDiscoveryService,
    )
    from pymobiledevice3.remote.tunnel_service import (
        get_core_device_tunnel_services,
        start_tunnel_over_core_device,
    )

    def _need_root() -> None:
        sys.exit(
            "RemoteXPC tunnel setup needs root on macOS\n"
            "(it stops `remoted` for Bonjour discovery and creates a utun).\n"
            "Re-run as: sudo gpsspoof ...\n"
            "Or install the tunneld daemon to skip sudo "
            "(see README: 'Skip sudo with tunneld')."
        )

    _progress("scanning for RemoteXPC service (Bonjour, ~3s)...")
    t0 = time.monotonic()
    try:
        services = await get_core_device_tunnel_services(udid=udid)
    except AccessDeniedError:
        _need_root()
    except OSError as e:
        if getattr(e, "errno", None) in (1, 13):  # EPERM/EACCES
            _need_root()
        raise
    if not services:
        sys.exit(
            "no RemoteXPC tunnel service found for this iPhone.\n"
            "Confirm Developer Mode is on, the device is unlocked & trusted,\n"
            "and the developer disk image is mounted (it ships with iOS 17+)."
        )
    service = next((s for s in services if s.rsd.udid == udid), services[0])
    _progress(f"found RemoteXPC service in {time.monotonic() - t0:.1f}s")

    try:
        _progress("establishing TCP tunnel...")
        t1 = time.monotonic()
        async with start_tunnel_over_core_device(
            service, protocol=TunnelProtocol.TCP
        ) as tr:
            _progress(f"tunnel up at {tr.address}:{tr.port} ({time.monotonic() - t1:.1f}s)")
            _progress("connecting to RemoteServiceDiscovery...")
            async with RemoteServiceDiscoveryService((tr.address, tr.port)) as rsd:
                yield rsd
    except AccessDeniedError:
        _need_root()
    except OSError as e:
        if getattr(e, "errno", None) in (1, 13):
            _need_root()
        raise
    except NoDeviceConnectedError:
        sys.exit("device disconnected before tunnel could come up")


@asynccontextmanager
async def open_rsd(udid: str):
    """Yield a connected RemoteServiceDiscoveryService for the given iPhone.

    Tries tunneld (no root needed) first; falls back to building the
    tunnel in-process (root required). The fallback chain means the
    same code works whether or not the user has installed tunneld as
    a launchd daemon.
    """
    rsd = await _try_tunneld(udid)
    if rsd is not None:
        try:
            yield rsd
        finally:
            try:
                await rsd.close()
            except Exception:
                pass
        return

    async with _open_rsd_in_process(udid) as rsd:
        yield rsd


@asynccontextmanager
async def open_dvt(udid: str):
    """Yield a connected DvtProvider for the given iPhone."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    async with open_rsd(udid) as rsd:
        _progress("opening DVT channel...")
        t = time.monotonic()
        async with DvtProvider(rsd) as dvt:
            _progress(f"DVT channel ready ({time.monotonic() - t:.1f}s)")
            yield dvt


def cmd_list(_args: argparse.Namespace) -> int:
    locations = load_locations()
    if not locations:
        print("(no locations defined)")
        return 0
    name_w = max(len(n) for n in locations)
    for name in sorted(locations):
        loc = locations[name]
        print(f"  {name:<{name_w}}  {float(loc['lat']):>10.4f}, {float(loc['lon']):>10.4f}")
    return 0


def _save_locations(locations: dict) -> None:
    path = locations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(locations, indent=2, sort_keys=True) + "\n")


def cmd_add(args: argparse.Namespace) -> int:
    if not (-90.0 <= args.lat <= 90.0):
        sys.exit(f"latitude must be in [-90, 90], got {args.lat}")
    if not (-180.0 <= args.lon <= 180.0):
        sys.exit(f"longitude must be in [-180, 180], got {args.lon}")
    locations = load_locations()
    existed = args.name in locations
    locations[args.name] = {"lat": args.lat, "lon": args.lon}
    _save_locations(locations)
    verb = "updated" if existed else "added"
    print(f"{verb} '{args.name}' ({args.lat}, {args.lon})")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    locations = load_locations()
    if args.name not in locations:
        sys.exit(f"no such location: '{args.name}'")
    del locations[args.name]
    _save_locations(locations)
    print(f"removed '{args.name}'")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    state = read_state()
    if state is None:
        print("no active spoof")
        return 0
    pid = state.get("pid")
    alive = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    if not alive:
        print("stale state (spoof process is gone); device may still have a")
        print("spoofed location set. Run `spoof clear` to fully reset.")
        print(f"  last: {state.get('name')} "
              f"({state.get('lat')}, {state.get('lon')}) "
              f"on {state.get('device_name')} [{state.get('udid')}]")
        return 0
    print("spoofing:")
    print(f"  device:   {state.get('device_name', '?')} [{state['udid']}]")
    print(f"  location: {state.get('name', '?')} "
          f"({state.get('lat')}, {state.get('lon')})")
    print(f"  pid:      {pid}")
    return 0


async def cmd_set(args: argparse.Namespace) -> int:
    _check_privileged_or_tunneld("set")
    locations = load_locations()
    if args.name not in locations:
        sys.exit(
            f"unknown location '{args.name}' "
            f"(available: {', '.join(sorted(locations))})"
        )
    loc = locations[args.name]
    lat = float(loc["lat"])
    lon = float(loc["lon"])

    iphone = await select_iphone(args.udid)
    print(
        f"connected: {iphone['device_name']} "
        f"({iphone['product_type']}) iOS {iphone['product_version']}"
    )
    print(f"udid:      {iphone['udid']}")

    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )

    async with open_dvt(iphone["udid"]) as dvt:
        async with LocationSimulation(dvt) as sim:
            await sim.set(lat, lon)
            started_at = time.monotonic()
            write_state({
                "udid": iphone["udid"],
                "device_name": iphone["device_name"],
                "name": args.name,
                "lat": lat,
                "lon": lon,
                "pid": os.getpid(),
            })
            print()
            print(f"  SPOOFING ACTIVE  →  {args.name}  ({lat}, {lon})")
            print(f"  device           →  {iphone['device_name']}")
            print(f"  pid              →  {os.getpid()}")
            print()
            print("  press Ctrl-C to clear and exit")
            print()

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass

            try:
                await stop.wait()
            finally:
                held = time.monotonic() - started_at
                print(f"\n... received stop signal after {held:.1f}s, clearing location...")
                try:
                    await sim.clear()
                    print("... cleared. real GPS resumed.")
                except Exception as e:
                    print(f"warning: clear failed: {e}", file=sys.stderr)
                write_state(None)
    return 0


async def _menu_prompt(locations: dict) -> Optional[str]:
    """Show numbered menu, return chosen location key or None to quit."""
    names = sorted(locations.keys())
    name_w = max(len(n) for n in names)
    while True:
        print()
        print(f"{ANSI['bold']}Select a location:{ANSI['reset']}")
        for i, name in enumerate(names, 1):
            loc = locations[name]
            print(f"  {ANSI['dim']}[{i:2d}]{ANSI['reset']}  "
                  f"{name:<{name_w}}  "
                  f"{float(loc['lat']):>9.4f}, {float(loc['lon']):>10.4f}")
        print(f"  {ANSI['dim']}[ q]{ANSI['reset']}  quit")
        print()
        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None, input, f"{ANSI['cyan']}>{ANSI['reset']} "
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        choice = raw.strip().lower()
        if not choice or choice in ("q", "quit", "exit"):
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(names):
                return names[idx - 1]
        if choice in locations:
            return choice
        print(f"  {ANSI['yellow']}'{raw.strip()}' is not a valid choice"
              f"{ANSI['reset']}")


async def _wait_for_key_with_ticker(started_at: float) -> str:
    """Hold until the user presses a key; tick an elapsed-time line in place.

    Returns 'quit' if the keypress was Ctrl-C (byte 0x03), else 'back'.
    Falls back to a blocking input() when stdin is not a tty.
    """
    fd = sys.stdin.fileno()
    is_tty = os.isatty(fd)
    loop = asyncio.get_event_loop()

    if not is_tty:
        # Non-tty stdin: just wait for a line. No tick (no point).
        try:
            await loop.run_in_executor(None, sys.stdin.readline)
        except KeyboardInterrupt:
            return "quit"
        return "back"

    old_attrs = termios.tcgetattr(fd)
    new_attrs = termios.tcgetattr(fd)
    # cbreak + no echo + no signal generation: Ctrl-C arrives as byte 0x03
    # so we can clean up before reacting, instead of having SIGINT cancel
    # us mid-clear.
    new_attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
    termios.tcsetattr(fd, termios.TCSADRAIN, new_attrs)

    future: asyncio.Future = loop.create_future()

    def on_readable() -> None:
        try:
            data = os.read(fd, 16)
        except Exception:
            data = b""
        if not future.done():
            future.set_result(data)

    loop.add_reader(fd, on_readable)

    async def ticker() -> None:
        while True:
            elapsed = int(time.monotonic() - started_at)
            h, m, s = elapsed // 3600, (elapsed // 60) % 60, elapsed % 60
            sys.stdout.write(
                f"\r  {ANSI['dim']}elapsed:{ANSI['reset']} "
                f"{h}:{m:02d}:{s:02d}  "
            )
            sys.stdout.flush()
            await asyncio.sleep(1)

    ticker_task = asyncio.create_task(ticker())
    try:
        data = await future
        # Wipe the elapsed line so the cleanup messages start clean.
        sys.stdout.write(ANSI["clr_line"])
        sys.stdout.flush()
        return "quit" if data and b"\x03" in data else "back"
    finally:
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


async def _interactive_session(
    iphone: dict, name: str, loc: dict
) -> bool:
    """Open tunnel, set the location, hold until keypress, clear.

    Returns True if the user wants to quit the whole UI (Ctrl-C),
    False to return to the menu.
    """
    lat = float(loc["lat"])
    lon = float(loc["lon"])

    print()
    print(f"{ANSI['bold']}→ engaging:{ANSI['reset']} {name} ({lat}, {lon})")

    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )

    quit_app = False
    async with open_dvt(iphone["udid"]) as dvt:
        async with LocationSimulation(dvt) as sim:
            await sim.set(lat, lon)
            started = time.monotonic()
            write_state({
                "udid": iphone["udid"],
                "device_name": iphone["device_name"],
                "name": name,
                "lat": lat,
                "lon": lon,
                "pid": os.getpid(),
            })

            print()
            print(f"  {ANSI['green']}{ANSI['bold']}┌─ SPOOFING ACTIVE"
                  f"{ANSI['reset']}")
            print(f"  {ANSI['green']}│{ANSI['reset']}  "
                  f"{name}  ({lat}, {lon})")
            print(f"  {ANSI['green']}│{ANSI['reset']}  "
                  f"{iphone['device_name']}  [{iphone['udid']}]")
            print(f"  {ANSI['green']}│{ANSI['reset']}")
            print(f"  {ANSI['green']}└─{ANSI['reset']} "
                  f"{ANSI['dim']}press any key to clear and return to menu  "
                  f"(Ctrl-C to quit){ANSI['reset']}")

            try:
                result = await _wait_for_key_with_ticker(started)
                quit_app = result == "quit"
            finally:
                print(f"  {ANSI['dim']}clearing...{ANSI['reset']}")
                try:
                    await sim.clear()
                except Exception as e:
                    print(f"  {ANSI['yellow']}warning: clear failed: "
                          f"{e}{ANSI['reset']}")
                write_state(None)
                print(f"  {ANSI['green']}cleared, real GPS resumed"
                      f"{ANSI['reset']}")
    return quit_app


async def cmd_ui(args: argparse.Namespace) -> int:
    _check_privileged_or_tunneld("ui")

    locations = load_locations()
    if not locations:
        sys.exit("no locations defined; add one with `gpsspoof add NAME LAT LON`")

    iphone = await select_iphone(args.udid)

    bar = "─" * 60
    print()
    print(bar)
    print(f"  {ANSI['bold']}gpsspoof{ANSI['reset']}  "
          f"{ANSI['dim']}interactive mode{ANSI['reset']}")
    print(f"  device:  {iphone['device_name']} "
          f"({iphone['product_type']}) iOS {iphone['product_version']}")
    print(f"  udid:    {iphone['udid']}")
    print(bar)

    try:
        while True:
            choice = await _menu_prompt(locations)
            if choice is None:
                break
            try:
                if await _interactive_session(iphone, choice, locations[choice]):
                    break  # Ctrl-C in active session → quit UI
            except KeyboardInterrupt:
                break
    except (KeyboardInterrupt, EOFError):
        pass

    print()
    print(f"{ANSI['dim']}bye.{ANSI['reset']}")
    return 0


async def cmd_clear(args: argparse.Namespace) -> int:
    _check_privileged_or_tunneld("clear")
    state = read_state()
    udid = args.udid or (state.get("udid") if state else None)
    iphone = await select_iphone(udid)
    print(
        f"connected: {iphone['device_name']} "
        f"({iphone['product_type']}) iOS {iphone['product_version']}"
    )

    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )

    async with open_dvt(iphone["udid"]) as dvt:
        async with LocationSimulation(dvt) as sim:
            await sim.clear()

    write_state(None)
    print(f"cleared spoofed location on {iphone['device_name']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpsspoof",
        description=(
            "Spoof GPS location on a USB-connected iPhone (iOS 17+).\n"
            "Run `gpsspoof` with no arguments to see this help."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Common usage:\n"
            "  gpsspoof ui                       # interactive menu (recommended)\n"
            "  gpsspoof list                     # show known locations\n"
            "  gpsspoof set seattle              # start spoofing (Ctrl-C to stop)\n"
            "  gpsspoof clear                    # stop any active spoof\n"
            "  gpsspoof status                   # show current state\n"
            "  gpsspoof add airport 47.4502 -122.3088\n"
            "  gpsspoof rm airport\n"
            "\n"
            "Note: `ui`, `set`, `clear` need either root (`sudo gpsspoof ...`)\n"
            "or a running tunneld daemon. See README: 'Skip sudo with tunneld'."
        ),
    )
    p.add_argument(
        "--udid",
        help="select a specific iPhone by UDID when more than one is connected",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("ui", help="interactive menu mode (needs sudo or tunneld)")
    sub.add_parser("list", help="list available named locations")

    p_set = sub.add_parser("set", help="start spoofing to a named location (needs sudo or tunneld)")
    p_set.add_argument("name", help="name from locations.json")

    sub.add_parser("clear", help="stop any active spoof on the device (needs sudo or tunneld)")
    sub.add_parser("status", help="show current spoofed location, if any")

    p_add = sub.add_parser("add", help="add or update a named location")
    p_add.add_argument("name")
    p_add.add_argument("lat", type=float)
    p_add.add_argument("lon", type=float)

    p_rm = sub.add_parser("rm", help="remove a named location")
    p_rm.add_argument("name")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd is None:
        parser.print_help()
        return 0
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "rm":
        return cmd_rm(args)
    if args.cmd == "set":
        try:
            return asyncio.run(cmd_set(args))
        except KeyboardInterrupt:
            return 130
    if args.cmd == "clear":
        try:
            return asyncio.run(cmd_clear(args))
        except KeyboardInterrupt:
            return 130
    if args.cmd == "ui":
        try:
            return asyncio.run(cmd_ui(args))
        except KeyboardInterrupt:
            return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
