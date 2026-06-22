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

Commands:

* `list`, `status`, `add`, `rm`, `routes` only read/write the local JSON
  config and query usbmuxd. They run unprivileged.
* `set` (fixed point), `route` (move through waypoints at a speed, with
  once/loop/bounce repeat and optional save/load), `map` (a localhost
  Leaflet UI to click a point or build/drive a route live), `ui`
  (interactive menu), and `clear` all reach the device, so each needs
  either root (to build the RemoteXPC tunnel — it pauses `remoted` and
  creates a TCP tunnel) or a running tunneld daemon that brokers one.

Movement (`route` and the `map` page) repeatedly calls `LocationSimulation
.set()` with positions interpolated between waypoints; `drive_route`
integrates distance from real elapsed time × the current speed and paces
updates adaptively (see `_update_interval`). Named routes persist to
`~/.config/iphone-spoof/routes.json` (shared by the CLI and the map).

Opt-in "realistic" motion (`route --realistic`, the map's "natural motion"
checkbox, or a `natural` keyword in the interactive `ui`) layers human-like
driving over that exact path via `MotionState`: the commanded speed becomes a
cruise target that wanders within a band, real acceleration/deceleration limits
smooth every change, the car brakes for upcoming corners and rolls to a stop on
arrival, and the reported fix carries drifting GPS jitter. The protocol only
transmits latitude/longitude — iOS synthesizes the `horizontalAccuracy` reading
itself, so it can't be set — but the jitter reproduces the *effect* of a
variable accuracy, and iOS derives the reported speed/course from the fixes, so
those look natural too.

The active spoof session is mirrored to `~/.config/iphone-spoof/state.json`
purely so `gpsspoof status` can describe what's running. The state file is
removed on clean exit; if the `set` process is killed with `kill -9`, the
file goes stale and the device may keep the simulated fix until the next
`gpsspoof clear`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import random
import re
import signal
import sys
import termios
import threading
import time
import tty
import webbrowser
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


def _progress(msg: str) -> None:
    """Print a stage line to stderr so it's visually distinct from results."""
    print(f"... {msg}", file=sys.stderr, flush=True)


TUNNELD_HOST = "127.0.0.1"
TUNNELD_PORT = 49151

# Speed handling for `route`. A bare number is interpreted as miles/hour.
MPH_TO_MPS = 0.44704
SPEED_UNITS = {
    "mph": MPH_TO_MPS,
    "kmh": 1000.0 / 3600.0,
    "km/h": 1000.0 / 3600.0,
    "kph": 1000.0 / 3600.0,
    "mps": 1.0,
    "m/s": 1.0,
}
DEFAULT_ROUTE_SPEED_MPH = 30.0
# Position-update pacing while driving a route. The interval adapts to the
# current speed: aim for ~GPS_STEP_TARGET_M between updates, but never slower
# than ROUTE_TICK_S (keeps low speeds smooth, as Apple Maps interpolates fine
# at 1s) nor faster than GPS_MIN_INTERVAL_S (so high speeds can't flood the
# device with set() calls). At 30 mph that's the 1s cadence (~13 m steps); at
# 300 mph it tightens to ~0.23s so each step stays ~100 ft.
ROUTE_TICK_S = 1.0
GPS_STEP_TARGET_M = 30.48   # ~100 feet: target distance moved per update
GPS_MIN_INTERVAL_S = 0.1    # at most ~10 updates/sec

# --- Realistic motion (opt-in: `route --realistic`, map "natural" toggle) ----
# Layers human-like driving and GPS noise over the exact path. The protocol can
# only send latitude/longitude (iOS synthesizes horizontalAccuracy itself, so
# the accuracy *number* isn't settable), but the *effect* of variable accuracy
# is reproduced as drifting positional jitter. There is one master toggle and
# no per-run knobs; these baked-in defaults are tuned to look natural without
# being distracting. Speeds are m/s, accelerations m/s^2, distances/jitter
# meters, correlation times seconds.
REALISM_ACCEL_MAX = 2.0        # comfortable acceleration
REALISM_DECEL_MAX = 3.0        # comfortable braking (harder than accel)
REALISM_RATE_JITTER = 0.30     # +/- variance applied to each tick's accel/decel
REALISM_SPEED_VARIATION = 0.08  # cruise speed wanders this fraction around target
REALISM_SPEED_TAU_S = 5.0      # how slowly the cruise speed drifts
REALISM_CORNER_EXP = 2.3       # turn-sharpness -> slowdown curve (bigger = sharper drop)
REALISM_CORNER_MIN = 0.12      # never slow below this fraction of cruise in a turn
REALISM_JITTER_M = 2.5         # baseline GPS accuracy radius (meters)
REALISM_JITTER_MAX_M = 8.0     # worst-case accuracy radius when it degrades
REALISM_JITTER_TAU_S = 2.5     # how quickly the scatter wanders (position noise)
REALISM_ACCURACY_TAU_S = 20.0  # how slowly the accuracy radius itself drifts
REALISM_JITTER_SHAPE = 0.5     # per-axis noise as a fraction of the accuracy radius

# `map` server: where to center the browser map when there's no prior fix.
MAP_DEFAULT_CENTER = (47.6062, -122.3321)  # Seattle
MAP_DEFAULT_ZOOM = 12


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


def routes_path() -> Path:
    return get_config_dir() / "routes.json"


def load_routes() -> dict:
    """Return the saved-routes map (name -> {points, speed, repeat}).

    `points` is a list of ``[lat, lon]`` pairs, `speed` is in mph, `repeat`
    is one of once/loop/bounce. Returns {} when no routes file exists yet.
    """
    path = routes_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {path}: {e}")
    if not isinstance(data, dict):
        sys.exit(f"{path} must contain a JSON object")
    return data


def save_routes(routes: dict) -> None:
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(routes, indent=2, sort_keys=True) + "\n")


def save_route(name: str, points: list, speed_mph: float, repeat: str) -> None:
    """Create or overwrite a saved route."""
    routes = load_routes()
    routes[name] = {"points": points, "speed": speed_mph, "repeat": repeat}
    save_routes(routes)


def delete_route(name: str) -> bool:
    """Delete a saved route; return True if it existed."""
    routes = load_routes()
    if name not in routes:
        return False
    del routes[name]
    save_routes(routes)
    return True


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


def parse_coords(text: str) -> Optional[tuple[float, float]]:
    """Parse "lat, lon" (or "lat lon") into a validated (lat, lon) pair.

    Returns None when the text isn't shaped like a coordinate pair, so the
    caller can fall back to a named-location lookup. Raises ValueError with a
    specific message when the text *is* a pair but a value is out of range —
    that's a typo worth reporting, not a name to look up.

    Accepts comma- and/or whitespace-separated forms so a pasted
    "47.490308, -122.205647" works as-is.
    """
    parts = [p for p in text.replace(",", " ").split() if p]
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0])
        lon = float(parts[1])
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"latitude must be in [-90, 90], got {lat}")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"longitude must be in [-180, 180], got {lon}")
    return (lat, lon)


def parse_speed(text: str) -> float:
    """Parse a speed string into meters/second.

    A bare number ("30") is miles/hour. A unit suffix overrides that:
    "48km/h", "13 m/s", "30mph". Raises ValueError on anything unparseable,
    non-positive, or in an unknown unit.
    """
    s = text.strip().lower()
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-z/]*)\s*", s)
    if not m:
        raise ValueError(f"could not parse speed: {text!r}")
    value = float(m.group(1))
    unit = m.group(2) or "mph"
    if unit not in SPEED_UNITS:
        raise ValueError(
            f"unknown speed unit '{unit}' (use one of: mph, km/h, m/s)"
        )
    if value <= 0:
        raise ValueError(f"speed must be positive, got {value}")
    return value * SPEED_UNITS[unit]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in meters."""
    radius = 6371000.0  # mean Earth radius
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def resolve_waypoint(token: str, locations: dict) -> tuple[str, float, float]:
    """Resolve one waypoint (a name or a "lat,lon" token) to (name, lat, lon).

    Raises ValueError if the token is neither valid coordinates nor a known
    location name.
    """
    token = token.strip()
    coords = parse_coords(token)  # raises ValueError on out-of-range coords
    if coords is not None:
        lat, lon = coords
        return (f"{lat}, {lon}", lat, lon)
    if token in locations:
        loc = locations[token]
        return (token, float(loc["lat"]), float(loc["lon"]))
    raise ValueError(
        f"unknown waypoint '{token}' "
        f"(not coordinates, and not a known location)"
    )


def resolve_waypoints(
    tokens: list[str], locations: dict
) -> list[tuple[str, float, float]]:
    """Resolve a list of waypoint tokens, requiring at least two."""
    points = [resolve_waypoint(t, locations) for t in tokens]
    if len(points) < 2:
        raise ValueError("a route needs at least two waypoints")
    return points


def parse_route_line(
    line: str, locations: dict
) -> tuple[list[tuple[str, float, float]], float, str, bool]:
    """Parse a route line into (waypoints, speed_mps, repeat, realistic).

    Format: ``WP > WP [> WP ...] [@ SPEED] [loop|bounce] [natural]`` where each
    WP is a name or a "lat,lon" pair, waypoints are separated by ``>`` or
    ``->``, the optional ``@ SPEED`` overrides the default speed (mph), an
    optional ``loop`` or ``bounce`` keyword sets the repeat mode (default a
    single pass), and an optional ``natural`` (or ``realistic``) keyword turns
    on human-like motion + GPS jitter. e.g.::

        kent > seattle > redmond @ 30
        47.5,-122.2 -> 47.49,-122.2 @ 48km/h
        kent > seattle > redmond @ 30 bounce
        kent > seattle > redmond @ 30 loop natural
    """
    line, n = re.subn(r"\s+(realistic|natural)\b", "", line, flags=re.IGNORECASE)
    realistic = bool(n)
    repeat = "once"
    m = re.search(r"\s+(loop|bounce)\s*$", line, re.IGNORECASE)
    if m:
        repeat = m.group(1).lower()
        line = line[:m.start()]
    speed_mps = DEFAULT_ROUTE_SPEED_MPH * MPH_TO_MPS
    if "@" in line:
        line, _, speed_str = line.rpartition("@")
        speed_mps = parse_speed(speed_str)
    tokens = [t.strip() for t in re.split(r"->|>", line) if t.strip()]
    return resolve_waypoints(tokens, locations), speed_mps, repeat, realistic


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


def route_total_m(points: list[tuple[str, float, float]]) -> float:
    """Sum of segment distances along a list of (name, lat, lon) waypoints."""
    return sum(
        haversine_m(points[i][1], points[i][2], points[i + 1][1], points[i + 1][2])
        for i in range(len(points) - 1)
    )


def route_pass_m(points: list[tuple[str, float, float]], repeat: str) -> float:
    """Distance of one repeat unit ("pass") for the given repeat mode.

    once   -> first to last (the plain route)
    loop   -> first to last plus the closing last->first leg (a full lap)
    bounce -> there and back, i.e. twice the route
    """
    base = route_total_m(points)
    if repeat == "loop":
        return base + haversine_m(
            points[-1][1], points[-1][2], points[0][1], points[0][2]
        )
    if repeat == "bounce":
        return base * 2
    return base


def _print_route_progress(
    seg_idx: int, n_segs: int, dest_name: str,
    lat: float, lon: float, travelled_m: float, total_m: float, speed_mps: float,
) -> None:
    """Render a single in-place progress line for an in-flight route."""
    pct = (travelled_m / total_m * 100.0) if total_m > 0 else 100.0
    remaining_m = max(0.0, total_m - travelled_m)
    eta_s = int(remaining_m / speed_mps) if speed_mps > 0 else 0
    sys.stdout.write(
        f"\r  {ANSI['dim']}seg {seg_idx + 1}/{n_segs} -> {dest_name}"
        f"{ANSI['reset']}  {lat:.5f}, {lon:.5f}  "
        f"{ANSI['dim']}{pct:5.1f}%  {speed_mps / MPH_TO_MPS:.0f} mph  "
        f"ETA {eta_s}s{ANSI['reset']}   "
    )
    sys.stdout.flush()


def _update_interval(speed_mps: float) -> float:
    """Seconds to wait before the next position update at this speed.

    Targets ~GPS_STEP_TARGET_M (about 100 ft) of travel between updates so
    movement stays smooth, but clamps the interval to
    [GPS_MIN_INTERVAL_S, ROUTE_TICK_S]: never slower than the 1s base cadence
    (low speeds), never faster than ~10/s (so high speeds don't overwhelm the
    device). A non-positive speed (paused/idle) just uses the base cadence.
    """
    if speed_mps <= 0:
        return ROUTE_TICK_S
    return min(ROUTE_TICK_S, max(GPS_MIN_INTERVAL_S, GPS_STEP_TARGET_M / speed_mps))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees [0,360)."""
    la1, la2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _corner_speed_factor(theta_rad: float) -> float:
    """Fraction of cruise speed to carry through a corner of deflection `theta`.

    `theta` is the heading change at the vertex (0 = straight through, pi = a
    U-turn). 0 deg -> 1.0 (no slowing); ~90 deg -> ~0.45; 180 deg -> the floor.
    """
    return max(REALISM_CORNER_MIN,
               math.cos(theta_rad / 2.0) ** REALISM_CORNER_EXP)


class MotionState:
    """Evolving kinematic + GPS-noise state for one realistic drive.

    Created once per drive and threaded through `drive_route` /
    `drive_repeated`, so the current speed and the (correlated) GPS jitter
    persist across segments and repeated passes instead of resetting. The
    key tunables (speed_variation, accel_max, decel_max, jitter_m,
    jitter_max_m) are constructor args defaulting to the module-level REALISM_*
    constants; the map UI overrides them per drive, the CLI/`ui` use defaults.

    The four behaviors:
      * cruise_speed   - the commanded speed becomes a target that wanders
        within a band (a mean-reverting random walk), so it never sits dead flat.
      * corner_limited_speed - look ahead to upcoming turns and cap the target
        so we brake (at most REALISM_DECEL_MAX) to a turn-appropriate speed in
        time, then accelerate back out.
      * ramp_to        - move the actual speed toward the target no faster than
        the accel/decel limits (with per-tick variance), so speed never jumps.
      * jitter_offset  - add slowly-drifting positional noise to the reported
        fix; the noise radius itself wanders between the baseline and worst-case
        scatter, which is how we simulate a variable GPS-accuracy reading.
    """

    def __init__(self, *, speed_variation: float = REALISM_SPEED_VARIATION,
                 accel_max: float = REALISM_ACCEL_MAX,
                 decel_max: float = REALISM_DECEL_MAX,
                 jitter_m: float = REALISM_JITTER_M,
                 jitter_max_m: float = REALISM_JITTER_MAX_M,
                 rng: Optional[random.Random] = None) -> None:
        # Per-drive tunables (defaults = the module REALISM_* constants). The map
        # UI overrides these per drive; the CLI/ui use the defaults. Clamped to
        # sane ranges so a stray value can't divide by zero or stall the drive.
        self.speed_variation = max(0.0, min(0.5, speed_variation))
        self.accel_max = max(0.1, accel_max)
        self.decel_max = max(0.1, decel_max)
        self.jitter_m = max(0.0, jitter_m)
        self.jitter_max_m = max(self.jitter_m, jitter_max_m)
        self.v = 0.0                  # current speed, m/s (starts from rest)
        self.cruise_mult = 1.0        # wandering multiplier on the commanded speed
        self.jx = 0.0                 # current east jitter offset, meters
        self.jy = 0.0                 # current north jitter offset, meters
        self.sigma = self.jitter_m    # current scatter radius, meters
        self.rng = rng or random.Random()

    def _ou_step(self, value: float, mean: float, tau: float,
                 std: float, dt: float) -> float:
        """One step of a mean-reverting (Ornstein-Uhlenbeck) random walk.

        Pulls `value` toward `mean` with time constant `tau` while injecting
        Gaussian noise scaled so the long-run standard deviation is ~`std`.
        Correlated over time (unlike per-tick white noise), which is what makes
        both the speed drift and the GPS scatter look natural rather than jittery.
        """
        if tau <= 0 or dt <= 0:
            return mean
        a = min(1.0, dt / tau)
        return value + (mean - value) * a + self.rng.gauss(0.0, std) * math.sqrt(2.0 * a)

    def cruise_speed(self, commanded: float, dt: float) -> float:
        """The commanded speed nudged by the wandering cruise multiplier."""
        if commanded <= 0:
            return 0.0       # paused / idle: target a full stop
        self.cruise_mult = self._ou_step(
            self.cruise_mult, 1.0, REALISM_SPEED_TAU_S,
            self.speed_variation, dt)
        lo, hi = 1.0 - 2.5 * self.speed_variation, 1.0 + 2.5 * self.speed_variation
        self.cruise_mult = min(hi, max(lo, self.cruise_mult))
        return commanded * self.cruise_mult

    def corner_limited_speed(self, cruise: float, cum: list[float],
                             theta: list[float], seg_idx: int,
                             travelled: float, stop_at_end: bool) -> float:
        """Cap `cruise` so we can brake in time for any upcoming corner/stop.

        Scans vertices ahead of the current position (cumulative distance
        `cum[seg_idx] + travelled`) within braking range. For each, the max
        speed we may travel now and still slow to its corner speed by then is
        ``sqrt(v_corner^2 + 2*decel*d)``; the smallest such limit wins. The
        final vertex is a stop when `stop_at_end` (a single pass arrives), else
        velocity is handed off to the next pass (loop/bounce stay in motion).
        """
        if cruise <= 0:
            return 0.0
        last = len(cum) - 1
        s_abs = cum[seg_idx] + travelled
        decel = self.decel_max
        horizon = cruise * cruise / (2.0 * decel) + 5.0
        v_target = cruise
        for k in range(seg_idx + 1, len(cum)):
            d = cum[k] - s_abs
            if d <= 0.0:
                continue
            if d > horizon:
                break
            if k == last:
                if not stop_at_end:
                    continue
                v_corner = 0.0
            else:
                v_corner = cruise * _corner_speed_factor(theta[k])
            v_allow = math.sqrt(v_corner * v_corner + 2.0 * decel * d)
            if v_allow < v_target:
                v_target = v_allow
        return v_target

    def ramp_to(self, v_target: float, dt: float) -> float:
        """Move the current speed toward `v_target`, accel/decel-limited."""
        if dt <= 0:
            return self.v
        a = self.accel_max if v_target > self.v else self.decel_max
        a *= 1.0 + self.rng.uniform(-REALISM_RATE_JITTER, REALISM_RATE_JITTER)
        dv = max(0.0, a) * dt
        if v_target > self.v:
            self.v = min(v_target, self.v + dv)
        else:
            self.v = max(v_target, self.v - dv)
        self.v = max(0.0, self.v)
        return self.v

    def jitter_offset(self, lat: float, dt: float) -> tuple[float, float]:
        """Correlated GPS noise to add to the *clean* fix, as (dlat, dlon).

        The caller adds this to the true path point each tick (never to the
        previous jittered fix), so the noise can't accumulate or walk the dot
        off course. `sigma` is the current accuracy radius, drifting slowly
        between the baseline and worst-case values (a varying "accuracy"); the
        east/north offsets are mean-reverting walks within it, and the combined
        offset is hard-bounded to `sigma` so the dot never strays farther from
        the true path than the reported accuracy — no wild excursions. Continues
        even at a standstill, so the dot wanders at rest like a real one.
        """
        mid = (self.jitter_m + self.jitter_max_m) / 2.0
        amp = (self.jitter_max_m - self.jitter_m) / 2.0
        self.sigma = min(self.jitter_max_m, max(
            self.jitter_m,
            self._ou_step(self.sigma, mid, REALISM_ACCURACY_TAU_S, amp, dt)))
        std = self.sigma * REALISM_JITTER_SHAPE
        self.jx = self._ou_step(self.jx, 0.0, REALISM_JITTER_TAU_S, std, dt)
        self.jy = self._ou_step(self.jy, 0.0, REALISM_JITTER_TAU_S, std, dt)
        # Bound the 2D offset to the accuracy radius (clamp the rare long tail).
        r = math.hypot(self.jx, self.jy)
        if r > self.sigma:
            self.jx *= self.sigma / r
            self.jy *= self.sigma / r
        dlat = self.jy / 111320.0
        dlon = self.jx / (111320.0 * math.cos(math.radians(lat)) or 1e-6)
        return dlat, dlon


def _realism_params_from_json(rp: dict) -> dict:
    """Coerce a map-supplied realism object into `MotionState` kwargs.

    All fields are optional; anything missing or unparseable falls back to the
    REALISM_* default, and `MotionState` clamps the result to sane ranges. The
    map sends canonical units: `speed_variation` as a fraction (0.08 = ±8%),
    `accel_max`/`decel_max` in m/s², `jitter_m`/`jitter_max_m` in meters.
    """
    def num(key: str, default: float) -> float:
        try:
            return float(rp[key])
        except (KeyError, TypeError, ValueError):
            return default
    return {
        "speed_variation": num("speed_variation", REALISM_SPEED_VARIATION),
        "accel_max": num("accel_max", REALISM_ACCEL_MAX),
        "decel_max": num("decel_max", REALISM_DECEL_MAX),
        "jitter_m": num("jitter_m", REALISM_JITTER_M),
        "jitter_max_m": num("jitter_max_m", REALISM_JITTER_MAX_M),
    }


async def drive_route(
    sim, points: list[tuple[str, float, float]], speed_mps: float,
    *, on_update=None, show_progress: bool = True, get_speed=None,
    motion: Optional[MotionState] = None, stop_at_end: bool = True,
) -> None:
    """Move the simulated location along `points` at `speed_mps`.

    Interpolates linearly between consecutive waypoints. Distance is integrated
    from real elapsed time times the current speed each tick, so `sim.set()`
    latency doesn't make the trip run slow — and a mid-trip speed change takes
    effect on the next tick. The gap between updates adapts to the speed (see
    `_update_interval`) to stay smooth without flooding the device.

    `get_speed`, if given, is called each tick to read the live speed (m/s);
    otherwise the fixed `speed_mps` is used. `on_update(lat, lon[, course,
    speed])` runs after each device update (the map server uses it to follow the
    dot); `course`/`speed` are only passed in realistic mode. `show_progress`
    prints the in-place terminal progress line.

    When `motion` is given the drive is "realistic": the commanded speed becomes
    a cruise target that wanders within a band, real acceleration/deceleration
    limits smooth every speed change, the car brakes for upcoming corners (and,
    when `stop_at_end`, rolls to a stop at the final waypoint), and the reported
    fix carries correlated GPS jitter. `motion` holds the velocity/noise state
    so it survives across segments and repeated passes. When `motion` is None the
    movement is exact (constant speed, no jitter) and `stop_at_end` is ignored.

    Returns once the final waypoint is reached, leaving the location set
    there (the caller decides whether to hold, loop, or clear).
    """
    total_m = route_total_m(points)
    n_segs = len(points) - 1

    # Realistic mode precomputes route geometry once: cumulative distance to
    # each vertex (for corner look-ahead), each segment's bearing (the reported
    # heading), and the deflection angle at each interior vertex (how sharp the
    # turn is -> how much to slow for it).
    cum: list[float] = []
    theta: list[float] = []
    seg_bearing: list[float] = []
    if motion is not None:
        cum = [0.0]
        for i in range(n_segs):
            cum.append(cum[i] + haversine_m(
                points[i][1], points[i][2],
                points[i + 1][1], points[i + 1][2]))
        seg_bearing = [
            _bearing_deg(points[i][1], points[i][2],
                         points[i + 1][1], points[i + 1][2])
            for i in range(n_segs)
        ]
        theta = [0.0] * len(points)
        for k in range(1, n_segs):
            d = abs(seg_bearing[k] - seg_bearing[k - 1]) % 360.0
            theta[k] = math.radians(min(d, 360.0 - d))

    done_m = 0.0
    for i in range(n_segs):
        _, lat0, lon0 = points[i]
        name1, lat1, lon1 = points[i + 1]
        seg_m = haversine_m(lat0, lon0, lat1, lon1)
        travelled = 0.0
        prev = time.monotonic()
        while True:
            now = time.monotonic()
            dt = now - prev
            prev = now
            if motion is not None:
                base = get_speed() if get_speed is not None else speed_mps
                cruise = motion.cruise_speed(max(0.0, base), dt)
                v_target = motion.corner_limited_speed(
                    cruise, cum, theta, i, travelled, stop_at_end)
                spd = motion.ramp_to(v_target, dt)
            else:
                spd = get_speed() if get_speed is not None else speed_mps
            travelled += max(0.0, spd) * dt
            frac = 1.0 if seg_m <= 0 else min(1.0, travelled / seg_m)
            lat = lat0 + (lat1 - lat0) * frac
            lon = lon0 + (lon1 - lon0) * frac
            if motion is not None:
                # Jitter is added to the clean path point (lat/lon), never to
                # the previous reported fix, so it can't accumulate or drift.
                dlat, dlon = motion.jitter_offset(lat, dt)
                rlat, rlon = lat + dlat, lon + dlon
                await sim.set(rlat, rlon)
                if on_update is not None:
                    on_update(rlat, rlon, seg_bearing[i], spd)
            else:
                rlat, rlon = lat, lon
                await sim.set(lat, lon)
                if on_update is not None:
                    on_update(lat, lon)
            if show_progress:
                _print_route_progress(
                    i, n_segs, name1, rlat, rlon, done_m + seg_m * frac,
                    total_m, spd,
                )
            if frac >= 1.0:
                break
            await asyncio.sleep(_update_interval(spd))
        done_m += seg_m


async def drive_repeated(
    sim, points: list[tuple[str, float, float]], speed_mps: float, repeat: str,
    *, on_update=None, show_progress: bool = True, get_speed=None,
    motion: Optional[MotionState] = None,
) -> None:
    """Drive `points` according to `repeat`.

    once   -> a single forward pass, then return.
    loop   -> forward, then drive the closing last->first leg, forever
              (A->B->C->D->E->A->B->...), so the dot travels a closed circuit.
    bounce -> forward then reverse, forever
              (A->B->C->D->E->D->C->B->A->B->...), reversing at each end.

    `loop` and `bounce` never return on their own; the caller stops them by
    cancelling this coroutine. `on_update` / `show_progress` / `motion` are
    forwarded to `drive_route`. In realistic mode only a single `once` pass
    rolls to a stop at the end; loop/bounce keep the velocity across passes
    (`stop_at_end=False`) so motion stays continuous between laps.
    """
    opts = {"on_update": on_update, "show_progress": show_progress,
            "get_speed": get_speed, "motion": motion}
    if repeat == "loop":
        cycle = points + [points[0]]  # close the lap by driving last->first
        while True:
            await drive_route(sim, cycle, speed_mps, stop_at_end=False, **opts)
    elif repeat == "bounce":
        reverse = list(reversed(points))
        while True:
            await drive_route(sim, points, speed_mps, stop_at_end=False, **opts)
            await drive_route(sim, reverse, speed_mps, stop_at_end=False, **opts)
    else:  # once
        await drive_route(sim, points, speed_mps, stop_at_end=True, **opts)


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


def resolve_target(tokens: list[str]) -> tuple[str, float, float]:
    """Resolve `set` arguments to a (display_name, lat, lon) triple.

    Accepts either a named location or raw coordinates::

        set seattle                   -> look up "seattle"
        set 47.490308 -122.205647     -> two-token coordinate pair
        set "47.490308, -122.205647"  -> one-token coordinate pair

    For raw coordinates the display name is just the coordinate string, which
    flows through to state.json and `gpsspoof status` unchanged.
    """
    text = " ".join(tokens)
    try:
        coords = parse_coords(text)
    except ValueError as e:
        sys.exit(str(e))
    if coords is not None:
        lat, lon = coords
        return (f"{lat}, {lon}", lat, lon)
    # Not coordinate-shaped, so it must be a single named location.
    if len(tokens) != 1:
        sys.exit(f"unrecognized location or coordinates: {text!r}")
    name = tokens[0]
    locations = load_locations()
    if name not in locations:
        sys.exit(
            f"unknown location '{name}' "
            f"(available: {', '.join(sorted(locations))})"
        )
    loc = locations[name]
    return (name, float(loc["lat"]), float(loc["lon"]))


async def cmd_set(args: argparse.Namespace) -> int:
    _check_privileged_or_tunneld("set")
    name, lat, lon = resolve_target(args.target)

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
                "name": name,
                "lat": lat,
                "lon": lon,
                "pid": os.getpid(),
            })
            print()
            print(f"  SPOOFING ACTIVE  →  {name}  ({lat}, {lon})")
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


async def cmd_route(args: argparse.Namespace) -> int:
    # --delete only edits the saved-routes file; no device needed.
    if args.delete:
        if delete_route(args.delete):
            print(f"deleted saved route '{args.delete}'")
            return 0
        sys.exit(f"no such saved route: '{args.delete}'")

    _check_privileged_or_tunneld("route")
    locations = load_locations()
    try:
        if args.load:
            saved = load_routes().get(args.load)
            if saved is None:
                sys.exit(f"no such saved route: '{args.load}' "
                         f"(see `gpsspoof routes`)")
            points = [(f"{a}, {b}", float(a), float(b))
                      for a, b in saved.get("points", [])]
            if len(points) < 2:
                sys.exit(f"saved route '{args.load}' has fewer than 2 stops")
            speed_mps = (parse_speed(args.speed) if args.speed is not None
                         else float(saved.get("speed", DEFAULT_ROUTE_SPEED_MPH))
                         * MPH_TO_MPS)
            repeat = args.repeat if args.repeat is not None \
                else saved.get("repeat", "once")
        else:
            if not args.waypoints:
                sys.exit("provide waypoints, or use --load NAME "
                         "(see `gpsspoof routes`)")
            speed_mps = (parse_speed(args.speed) if args.speed is not None
                         else DEFAULT_ROUTE_SPEED_MPH * MPH_TO_MPS)
            points = resolve_waypoints(args.waypoints, locations)
            repeat = args.repeat if args.repeat is not None else "once"
    except (ValueError, TypeError) as e:
        sys.exit(str(e))

    if repeat not in ("once", "loop", "bounce"):
        sys.exit(f"invalid repeat mode '{repeat}'")

    if args.save:
        save_route(args.save, [[la, lo] for _, la, lo in points],
                   round(speed_mps / MPH_TO_MPS, 4), repeat)
        print(f"saved route '{args.save}' ({len(points)} stops)")

    iphone = await select_iphone(args.udid)
    print(
        f"connected: {iphone['device_name']} "
        f"({iphone['product_type']}) iOS {iphone['product_version']}"
    )
    print(f"udid:      {iphone['udid']}")

    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )

    route_label = " -> ".join(p[0] for p in points)
    pass_m = route_pass_m(points, repeat)
    eta_s = int(pass_m / speed_mps) if speed_mps > 0 else 0
    mode_note = {"loop": "  (looping)", "bounce": "  (bouncing)"}.get(
        repeat, ""
    )
    per = " per pass" if repeat != "once" else ""
    motion = MotionState() if args.realistic else None
    print()
    print(f"  route:  {route_label}")
    print(f"  speed:  {speed_mps / MPH_TO_MPS:.1f} mph{mode_note}")
    print(f"  length: {pass_m / 1609.344:.2f} mi  (~{eta_s}s{per})")
    if motion is not None:
        print("  motion: natural (variable speed, accel/braking, "
              "corner slowdown, GPS jitter)")
    print()
    print("  press Ctrl-C to clear and exit")
    print()

    async with open_dvt(iphone["udid"]) as dvt:
        async with LocationSimulation(dvt) as sim:
            started_at = time.monotonic()
            write_state({
                "udid": iphone["udid"],
                "device_name": iphone["device_name"],
                "name": route_label,
                "lat": points[0][1],
                "lon": points[0][2],
                "pid": os.getpid(),
            })

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass

            run_task = asyncio.create_task(
                drive_repeated(sim, points, speed_mps, repeat, motion=motion)
            )
            stop_task = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait(
                    {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if run_task.done():
                    exc = run_task.exception()
                    if exc is not None:
                        raise exc
                    if not stop.is_set():
                        # Only `once` completes on its own: hold at the final
                        # waypoint until the user stops us, mirroring `set`.
                        print(f"\n  arrived at {points[-1][0]}; holding. "
                              f"Ctrl-C to clear and exit.")
                        await stop.wait()
            finally:
                # Drain both helper tasks without letting their
                # results/exceptions (incl. an error from `_run`, which is
                # re-raised above) prevent the clear below.
                run_task.cancel()
                stop_task.cancel()
                await asyncio.gather(
                    run_task, stop_task, return_exceptions=True
                )
                held = time.monotonic() - started_at
                print(f"\n... stopping after {held:.1f}s, clearing location...")
                try:
                    await sim.clear()
                    print("... cleared. real GPS resumed.")
                except Exception as e:
                    print(f"warning: clear failed: {e}", file=sys.stderr)
                write_state(None)
    return 0


def cmd_routes(_args: argparse.Namespace) -> int:
    routes = load_routes()
    if not routes:
        print("(no saved routes; build one with `gpsspoof map` or "
              "`gpsspoof route ... --save NAME`)")
        return 0
    name_w = max(len(n) for n in routes)
    for name in sorted(routes):
        r = routes[name]
        pts = r.get("points", [])
        print(f"  {name:<{name_w}}  {len(pts):>2} stops, "
              f"{r.get('speed', DEFAULT_ROUTE_SPEED_MPH)} mph, "
              f"{r.get('repeat', 'once')}")
    return 0


async def _wait_for_iphone(udid_filter: Optional[str]) -> dict:
    """Block until exactly one (matching) iPhone is connected, then return it.

    First poll is silent so an already-connected device returns instantly.
    On miss, prints a 'waiting' banner and ticks an elapsed-time line in
    place once a second. Exits with the standard multi-iPhone error if
    more than one matches and the caller didn't disambiguate.
    """
    def _filter(devices: list[dict]) -> list[dict]:
        return [d for d in devices if not udid_filter or d["udid"] == udid_filter]

    def _multi_error(devices: list[dict]) -> None:
        msg = ["multiple iPhones connected; specify --udid:"]
        for d in devices:
            msg.append(
                f"  {d['udid']}  {d['device_name']}  "
                f"({d['product_type']}, iOS {d['product_version']})"
            )
        sys.exit("\n".join(msg))

    iphones = _filter(await list_iphones())
    if len(iphones) == 1:
        return iphones[0]
    if len(iphones) > 1:
        _multi_error(iphones)

    # No iPhone yet — show waiting state and poll.
    print()
    label = f"udid={udid_filter}" if udid_filter else "any iPhone"
    print(f"  {ANSI['yellow']}waiting for {label} to be connected over USB..."
          f"{ANSI['reset']}")
    print(f"  {ANSI['dim']}(plug the phone in and unlock; Ctrl-C to cancel)"
          f"{ANSI['reset']}")

    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(1)
            elapsed = int(time.monotonic() - started)
            sys.stdout.write(
                f"\r  {ANSI['dim']}polling usbmuxd... "
                f"{elapsed}s{ANSI['reset']}  "
            )
            sys.stdout.flush()

            iphones = _filter(await list_iphones())
            if len(iphones) == 1:
                sys.stdout.write(ANSI["clr_line"])
                sys.stdout.flush()
                return iphones[0]
            if len(iphones) > 1:
                sys.stdout.write(ANSI["clr_line"])
                sys.stdout.flush()
                _multi_error(iphones)
    finally:
        # Ensure the spinner line is wiped on any exit path (Ctrl-C, etc.)
        sys.stdout.write(ANSI["clr_line"])
        sys.stdout.flush()


async def _menu_prompt(locations: dict) -> Optional[dict]:
    """Show numbered menu; return a selection dict, or None to quit.

    A selection is one of::

        {"kind": "point", "name": str, "loc": {"lat": .., "lon": ..}}
        {"kind": "route", "points": [(name, lat, lon), ...], "speed": mps}

    Beyond the numbered entries the user can type a raw coordinate pair
    ("47.490308, -122.205647") for a single point, or a ``>``-separated
    route with an optional ``@ speed`` ("kent > seattle > redmond @ 30").
    """
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
        print(f"  {ANSI['dim']}...or a coordinate pair: "
              f"47.490308, -122.205647{ANSI['reset']}")
        print(f"  {ANSI['dim']}...or a route to drive: "
              f"kent > seattle > redmond @ 30 [loop|bounce] [natural]"
              f"{ANSI['reset']}")
        print()
        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None, input, f"{ANSI['cyan']}>{ANSI['reset']} "
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        choice = raw.strip()
        low = choice.lower()
        if not choice or low in ("q", "quit", "exit"):
            return None
        if low.isdigit():
            idx = int(low)
            if 1 <= idx <= len(names):
                nm = names[idx - 1]
                return {"kind": "point", "name": nm, "loc": locations[nm]}
            print(f"  {ANSI['yellow']}'{choice}' is out of range{ANSI['reset']}")
            continue
        if ">" in choice:
            try:
                points, speed, repeat, realistic = parse_route_line(
                    choice, locations)
            except ValueError as e:
                print(f"  {ANSI['yellow']}{e}{ANSI['reset']}")
                continue
            return {"kind": "route", "points": points, "speed": speed,
                    "repeat": repeat, "realistic": realistic}
        try:
            coords = parse_coords(choice)
        except ValueError as e:
            print(f"  {ANSI['yellow']}{e}{ANSI['reset']}")
            continue
        if coords is not None:
            lat, lon = coords
            return {"kind": "point", "name": f"{lat}, {lon}",
                    "loc": {"lat": lat, "lon": lon}}
        if choice in locations:
            return {"kind": "point", "name": choice, "loc": locations[choice]}
        print(f"  {ANSI['yellow']}'{choice}' is not a valid choice"
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


async def _interactive_route(
    iphone: dict, points: list[tuple[str, float, float]], speed_mps: float,
    repeat: str = "once", realistic: bool = False,
) -> bool:
    """Drive a route in the UI, hold at the end, clear on key/Ctrl-C.

    Returns True to quit the whole UI (Ctrl-C), False to return to the menu.
    For a single pass, any key after arrival returns to the menu. For `loop`
    or `bounce` the route runs until Ctrl-C, which quits the UI (as does
    Ctrl-C while a single pass is still moving). `realistic` adds human-like
    acceleration/cornering and GPS jitter (see `MotionState`).
    """
    route_label = " -> ".join(p[0] for p in points)
    pass_m = route_pass_m(points, repeat)
    eta_s = int(pass_m / speed_mps) if speed_mps > 0 else 0
    mode_note = {"loop": ", looping", "bounce": ", bouncing"}.get(repeat, "")
    if realistic:
        mode_note += ", natural"
    motion = MotionState() if realistic else None

    print()
    print(f"{ANSI['bold']}→ driving:{ANSI['reset']} {route_label}")
    print(f"  {ANSI['dim']}{speed_mps / MPH_TO_MPS:.0f} mph, "
          f"{pass_m / 1609.344:.2f} mi, ~{eta_s}s{mode_note}{ANSI['reset']}")

    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )

    quit_app = False
    async with open_dvt(iphone["udid"]) as dvt:
        async with LocationSimulation(dvt) as sim:
            started = time.monotonic()
            write_state({
                "udid": iphone["udid"],
                "device_name": iphone["device_name"],
                "name": route_label,
                "lat": points[0][1],
                "lon": points[0][2],
                "pid": os.getpid(),
            })
            try:
                if repeat == "once":
                    await drive_route(sim, points, speed_mps, motion=motion)
                    print(f"\n  {ANSI['green']}arrived at {points[-1][0]}"
                          f"{ANSI['reset']}")
                    print(f"  {ANSI['dim']}press any key to clear and return "
                          f"to menu  (Ctrl-C to quit){ANSI['reset']}")
                    result = await _wait_for_key_with_ticker(started)
                    quit_app = result == "quit"
                else:
                    print(f"  {ANSI['dim']}Ctrl-C to clear and quit"
                          f"{ANSI['reset']}")
                    # Runs until Ctrl-C raises KeyboardInterrupt here.
                    await drive_repeated(sim, points, speed_mps, repeat,
                                         motion=motion)
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

    bar = "─" * 60
    print()
    print(bar)
    print(f"  {ANSI['bold']}gpsspoof{ANSI['reset']}  "
          f"{ANSI['dim']}interactive mode{ANSI['reset']}")
    print(bar)

    try:
        iphone = await _wait_for_iphone(args.udid)
    except (KeyboardInterrupt, EOFError):
        print()
        print(f"{ANSI['dim']}bye.{ANSI['reset']}")
        return 0

    print(f"  {ANSI['green']}connected{ANSI['reset']}")
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
                if choice["kind"] == "route":
                    done = await _interactive_route(
                        iphone, choice["points"], choice["speed"],
                        choice["repeat"], choice.get("realistic", False)
                    )
                else:
                    done = await _interactive_session(
                        iphone, choice["name"], choice["loc"]
                    )
                if done:
                    break  # Ctrl-C in active session → quit UI
            except KeyboardInterrupt:
                break
    except (KeyboardInterrupt, EOFError):
        pass

    print()
    print(f"{ANSI['dim']}bye.{ANSI['reset']}")
    return 0


CAR_DISPLAY_MAX_PX = 48     # longest side of the position-marker car icon
BADGE_DISPLAY_MAX_PX = 40   # longest side of the walking / standing badges
# Used only if assets/Corvette.png can't be read; points north (nose up).
CAR_FALLBACK_SVG = (
    '<svg width="36" height="36" viewBox="0 0 36 36">'
    '<path d="M18 1.5C21.5 1.5 23.5 6 23.5 12L23.5 25C23.5 31 21.5 34.5 18 '
    '34.5C14.5 34.5 12.5 31 12.5 25L12.5 12C12.5 6 14.5 1.5 18 1.5Z" '
    'fill="#ffffff" stroke="#2a2a2a" stroke-width="1.2" stroke-linejoin="round"/>'
    '<rect x="8.5" y="9" width="4" height="6" rx="1.5" fill="#1a1a1a"/>'
    '<rect x="23.5" y="9" width="4" height="6" rx="1.5" fill="#1a1a1a"/>'
    '<rect x="8.5" y="21" width="4" height="6" rx="1.5" fill="#1a1a1a"/>'
    '<rect x="23.5" y="21" width="4" height="6" rx="1.5" fill="#1a1a1a"/>'
    '<path d="M14.5 10L21.5 10L20.5 15L15.5 15Z" fill="#9bc4ea"/>'
    '<path d="M15.5 23L20.5 23L21.5 27L14.5 27Z" fill="#9bc4ea"/></svg>'
)


def _png_size(data: bytes) -> Optional[tuple[int, int]]:
    """Return (width, height) from a PNG's IHDR header, or None if not a PNG."""
    if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"):
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    return None


def _img_icon_html(filename: str, max_px: int,
                   fallback: tuple[str, str, str] | None = None) -> tuple[str, str, str]:
    """Build a marker's (inner HTML, iconSize, iconAnchor) from assets/<filename>.

    Embeds the PNG as a base64 data-URI (kept aspect-correct, longest side max_px);
    returns `fallback` (or an empty glyph) if the image can't be read. The size/anchor
    strings are JS array literals for the Leaflet divIcon.
    """
    path = Path(__file__).resolve().parent / "assets" / filename
    try:
        data = path.read_bytes()
        size = _png_size(data)
        if size:
            w, h = size
            scale = max_px / max(w, h)
            dw, dh = round(w * scale), round(h * scale)
        else:
            dw = dh = max_px
        b64 = base64.b64encode(data).decode("ascii")
        img = (f'<img src="data:image/png;base64,{b64}" '
               f'width="{dw}" height="{dh}"/>')
        return img, f"[{dw}, {dh}]", f"[{dw / 2}, {dh / 2}]"
    except OSError:
        if fallback:
            return fallback
        return "", f"[{max_px}, {max_px}]", f"[{max_px / 2}, {max_px / 2}]"


def car_icon_html() -> tuple[str, str, str]:
    """The car marker glyph (assets/Corvette.png; SVG car fallback)."""
    return _img_icon_html("Corvette.png", CAR_DISPLAY_MAX_PX,
                          (CAR_FALLBACK_SVG, "[36, 36]", "[18, 18]"))


# Single-page Leaflet map served by `gpsspoof map`. The __LAT__/__LON__/
# __ZOOM__/__ACTIVE__ tokens are substituted at serve time. Tiles come from
# OpenStreetMap. "Set point" mode POSTs clicks to /set; "Route" mode collects
# stops and POSTs them to /route; the page polls /state to follow the dot.
MAP_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>gpsspoof map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  #map { height: 100%; width: 100%; }
  #status {
    position: absolute; z-index: 1000; top: 10px; left: 50%;
    transform: translateX(-50%);
    background: rgba(0,0,0,0.78); color: #fff;
    font: 14px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
    padding: 8px 14px; border-radius: 8px; pointer-events: none;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3); white-space: nowrap;
  }
  #status.ok { background: rgba(20,120,40,0.92); }
  #status.pending { background: rgba(150,110,0,0.92); }
  #status.err { background: rgba(160,30,30,0.94); }
  #panel {
    position: absolute; z-index: 1000; top: 10px; left: 10px;
    background: rgba(255,255,255,0.96); color: #222;
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
    padding: 10px 12px; border-radius: 10px; min-width: 200px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.3);
  }
  #panel h1 { font-size: 12px; margin: 0 0 6px; letter-spacing: .12em; color: #555; }
  #panel .row { margin: 5px 0; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  #panel button { font: inherit; padding: 3px 10px; border-radius: 6px; border: 1px solid #bbb; background: #f4f4f4; cursor: pointer; }
  #panel button:hover { background: #e9e9e9; }
  #panel button.primary { background: #1462ff; color: #fff; border-color: #1462ff; }
  #panel input, #panel select { font: inherit; padding: 2px 4px; }
  #panel .muted { color: #666; }
  #panel .hint { font-size: 11px; color: #888; }
  #panel .sep { margin-top: 8px; padding-top: 8px; border-top: 1px solid #ddd; }
  .stop-icon { background: transparent; border: 0; }
  .stop-bubble {
    width: 22px; height: 22px; line-height: 22px; text-align: center;
    border-radius: 50%; background: #1462ff; color: #fff;
    font-weight: 700; font-size: 12px; border: 2px solid #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,0.45); cursor: move;
  }
  .stop-bubble.sel {
    background: #ff7a00;
    box-shadow: 0 0 0 3px rgba(255,122,0,0.45), 0 1px 4px rgba(0,0,0,0.45);
  }
  .leaflet-marker-icon { cursor: move; }      /* draggable markers */
  .route-seg { cursor: copy; }                /* click a segment to insert a stop */
  .car-icon { background: transparent; border: 0; }
  .car { transform-origin: 50% 50%; transition: transform 0.15s linear; line-height: 0; }
  .car svg, .car img { display: block; filter: drop-shadow(0 1px 2px rgba(0,0,0,0.45)); }
</style>
</head>
<body>
<div id="panel">
  <h1>GPSSPOOF</h1>
  <div class="row">
    <label><input type="radio" name="mode" value="set" checked> Set point</label>
    <label><input type="radio" name="mode" value="route"> Route</label>
  </div>
  <div class="row">
    speed <input id="speed" type="number" value="30" min="5" step="5" style="width:4.5em"> mph
    <select id="repeat">
      <option value="once">once</option>
      <option value="loop">loop</option>
      <option value="bounce">bounce</option>
    </select>
  </div>
  <div class="row">
    <label title="human-like speed, acceleration, cornering, and GPS jitter"><input type="checkbox" id="realistic"> natural motion</label>
  </div>
  <div id="realismOpts" style="display:none">
    <div class="row">
      vary &plusmn;<input id="rv_var" type="number" value="8" min="0" max="50" step="1" style="width:3em">%
      &nbsp;jitter <input id="rv_jmin" type="number" value="2.5" min="0" step="0.5" style="width:3em">&ndash;<input id="rv_jmax" type="number" value="8" min="0" step="0.5" style="width:3em">m
    </div>
    <div class="row">
      accel <input id="rv_acc" type="number" value="0.2" min="0.02" step="0.05" style="width:3em">g
      &nbsp;brake <input id="rv_dec" type="number" value="0.3" min="0.02" step="0.05" style="width:3em">g
    </div>
  </div>
  <div class="row">
    <span id="stops" class="muted">stops: 0</span>
    <button id="undo">Undo</button>
    <button id="clearBtn">Clear</button>
  </div>
  <div class="row" id="routeInfo" style="font-weight:600; color:#1462ff">add 2+ stops</div>
  <div class="row" id="liveHud" style="display:none; font-weight:700; color:#0a7d2c">&nbsp;</div>
  <div class="row hint">click map: add at end &middot; click a stop: select (Esc clears)</div>
  <div class="row hint">selected &rarr; click map inserts before it &middot; Del removes it</div>
  <div class="row hint">drag: move &middot; right-click: delete &middot; click line: insert</div>
  <div class="row">
    <button id="playStop" class="primary">&#9654; Drive</button>
    <button id="pauseBtn" disabled>&#9208; Pause</button>
    <label style="margin-left:auto"><input type="checkbox" id="follow"> follow</label>
  </div>
  <div class="row sep">
    <select id="routeSel" style="max-width:8.5em"><option value="">saved routes...</option></select>
    <button id="loadBtn">Load</button>
    <button id="delBtn">Del</button>
  </div>
  <div class="row">
    <input id="routeName" placeholder="name" style="width:7em">
    <button id="saveBtn">Save</button>
  </div>
</div>
<div id="status">loading...</div>
<div id="map"></div>
<script>
  const initial = { lat: __LAT__, lon: __LON__, active: __ACTIVE__ };
  const statusEl = document.getElementById('status');
  const map = L.map('map').setView([initial.lat, initial.lon], __ZOOM__);
  map.zoomControl.setPosition('topright');   // keep the +/- clear of the panel
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  // Position marker: a car when driving, a (direction-facing) walker when moving slowly, a standing
  // figure when not moving. All three share the rotatable/flippable '.car' wrapper.
  const ICONS = {
    car:   L.divIcon({ className: 'car-icon', html: '<div class="car">__CAR_IMG__</div>',   iconSize: __CAR_SIZE__,   iconAnchor: __CAR_ANCHOR__ }),
    walk:  L.divIcon({ className: 'car-icon', html: '<div class="car">__WALK_IMG__</div>',  iconSize: __WALK_SIZE__,  iconAnchor: __WALK_ANCHOR__ }),
    stand: L.divIcon({ className: 'car-icon', html: '<div class="car">__STAND_IMG__</div>', iconSize: __STAND_SIZE__, iconAnchor: __STAND_ANCHOR__ }),
  };
  const device = L.marker([initial.lat, initial.lon], { draggable: true, icon: ICONS.stand }).addTo(map);
  function glyphEl() { const e = device.getElement(); return e ? e.querySelector('.car') : null; }
  let carHeading = 0, youKind = 'stand', youFace = 'R';
  const MOVE_MIN_M = 1, DRIVE_MIN_M = 4;   // per ~1s poll: < move = standing, >= drive = car, else walking
  const STAND_MS = 0.5, DRIVE_MS = 2.5;    // realistic mode: m/s thresholds for standing / walking / driving
  function applyGlyph() {                   // car rotates to heading; walker flips to face travel; stand upright
    const el = glyphEl(); if (!el) return;
    el.style.transform = (youKind === 'car') ? ('rotate(' + carHeading + 'deg)')
                       : (youKind === 'walk' && youFace === 'L') ? 'scaleX(-1)' : 'none';
  }
  function setKind(kind) { if (kind !== youKind) { youKind = kind; device.setIcon(ICONS[kind]); } applyGlyph(); }
  function turnTo(b) { carHeading += ((b - carHeading) % 360 + 540) % 360 - 180; }  // turn the short way (no wild spin)
  function updateMarker(np, speed, course) {  // pick standing/walking/driving + heading
    if (typeof speed === 'number' && typeof course === 'number') {
      // realistic drive: the server sends the clean speed/heading, so GPS
      // jitter in the position doesn't make the marker spin or flicker kind.
      if (speed < STAND_MS) { setKind('stand'); }   // stopped -> standing; keep last heading
      else {
        turnTo(course);
        youFace = (course > 180) ? 'L' : 'R';        // heading west -> mirror the walker to face left
        setKind(speed >= DRIVE_MS ? 'car' : 'walk');
      }
      lastCarPos = np;
      return;
    }
    if (lastCarPos) {                         // exact drive: infer from movement since last fix
      const d = haversineM(lastCarPos, np);
      if (d < MOVE_MIN_M) { setKind('stand'); }   // not moving -> standing; keep last heading
      else {
        turnTo(bearing(lastCarPos, np));
        youFace = (bearing(lastCarPos, np) > 180) ? 'L' : 'R';
        setKind(d >= DRIVE_MIN_M ? 'car' : 'walk');
      }
    }
    lastCarPos = np;
  }
  function bearing(a, b) {
    const rad = Math.PI / 180, deg = 180 / Math.PI;
    const la1 = a.lat * rad, la2 = b.lat * rad, dlon = (b.lon - a.lon) * rad;
    const y = Math.sin(dlon) * Math.cos(la2);
    const x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(dlon);
    return (Math.atan2(y, x) * deg + 360) % 360;
  }
  let mode = 'set';
  let stops = [];
  let stopMarkers = [];
  let lines = [];
  let layerClickAt = 0;
  let selectedIndex = null;
  let driving = false;
  let paused = false;
  let lastCarPos = null;
  let lastSpeed = null, lastSpeedT = 0, accelG = 0;   // for the live speed/accel HUD
  const G_MS2 = 9.80665;
  const liveHud = document.getElementById('liveHud');
  const realismOpts = document.getElementById('realismOpts');

  function fmt(lat, lon) { return lat.toFixed(6) + ', ' + lon.toFixed(6); }
  function show(text, cls) { statusEl.textContent = text; statusEl.className = cls || ''; }
  function haversineM(a, b) {
    const R = 6371000, rad = Math.PI / 180;
    const dphi = (b.lat - a.lat) * rad, dlam = (b.lon - a.lon) * rad;
    const la1 = a.lat * rad, la2 = b.lat * rad;
    const x = Math.sin(dphi / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dlam / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function currentStops() {
    // live marker positions while dragging, else the committed stops
    if (stopMarkers.length === stops.length && stopMarkers.length) {
      return stopMarkers.map(function (mk) { const p = mk.getLatLng(); return { lat: p.lat, lon: p.lng }; });
    }
    return stops;
  }
  function routeMeters() {
    const p = currentStops(); let m = 0;
    for (let i = 0; i < p.length - 1; i++) m += haversineM(p[i], p[i + 1]);
    return m;
  }
  function fmtDur(s) {
    s = Math.round(s);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60), sec = s % 60;
    if (m < 60) return m + 'm ' + (sec < 10 ? '0' : '') + sec + 's';
    const h = Math.floor(m / 60), mm = m % 60;
    return h + 'h ' + (mm < 10 ? '0' : '') + mm + 'm';
  }
  function curSpeed() { return Number(document.getElementById('speed').value) || 0; }
  function realismParams() {        // natural-mode knobs, sent to the server in canonical units
    function num(id, d) { const v = Number(document.getElementById(id).value); return isFinite(v) ? v : d; }
    return {
      speed_variation: num('rv_var', 8) / 100,    // percent -> fraction
      accel_max: num('rv_acc', 0.2) * G_MS2,      // g -> m/s^2
      decel_max: num('rv_dec', 0.3) * G_MS2,      // g -> m/s^2
      jitter_m: num('rv_jmin', 2.5),
      jitter_max_m: num('rv_jmax', 8)
    };
  }
  function updateHud(speed, wasDriving) {   // live speed (mph) + acceleration (g) during a natural drive
    if (typeof speed !== 'number') { liveHud.style.display = 'none'; lastSpeed = null; return; }
    const now = performance.now();
    if (!wasDriving) { lastSpeed = null; accelG = 0; }   // fresh readout at drive start
    if (lastSpeed !== null && now > lastSpeedT) {
      const a = (speed - lastSpeed) / ((now - lastSpeedT) / 1000);   // m/s^2 over the poll gap
      accelG = accelG * 0.5 + (a / G_MS2) * 0.5;                      // light smoothing
    }
    lastSpeed = speed; lastSpeedT = now;
    const mph = speed / 0.44704;
    liveHud.textContent = mph.toFixed(0) + ' mph  ·  ' + (accelG >= 0 ? '+' : '') + accelG.toFixed(2) + ' g';
    liveHud.style.display = '';
  }
  function updateInfo() {
    const el = document.getElementById('routeInfo');
    if (stops.length < 2) { el.textContent = 'add 2+ stops'; return; }
    const m = routeMeters(), spd = curSpeed();
    const mi = (m / 1609.344).toFixed(2);
    const t = spd > 0 ? '~' + fmtDur(m / (spd * 0.44704)) : '--';
    el.textContent = mi + ' mi  ·  ' + t + ' one-way';
  }
  if (initial.active) { show('spoofing: ' + fmt(initial.lat, initial.lon), 'ok'); }
  else { show('real GPS - click the map (Set point) to spoof', ''); }

  async function post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).error || msg; } catch (e) {}
      throw new Error(msg);
    }
    return r.json().catch(function () { return {}; });
  }

  async function teleport(lat, lon) {
    lastCarPos = { lat: lat, lon: lon };   // so the next drive heads correctly
    setKind('stand');                      // a set point isn't moving -> standing
    show('sending: ' + fmt(lat, lon), 'pending');
    try { await post('/set', { lat: lat, lon: lon }); show('spoofing: ' + fmt(lat, lon), 'ok'); }
    catch (e) { show('error: ' + e.message, 'err'); }
  }

  function stopIcon(n, selected) {
    return L.divIcon({
      className: 'stop-icon',
      html: '<div class="stop-bubble' + (selected ? ' sel' : '') + '">' + n + '</div>',
      iconSize: [22, 22], iconAnchor: [11, 11]
    });
  }
  function removeStop(i) {
    stops.splice(i, 1);
    if (selectedIndex !== null) {
      if (i === selectedIndex) selectedIndex = null;
      else if (i < selectedIndex) selectedIndex--;
    }
    redrawRoute();
  }
  function updateLine() {
    lines.forEach(function (l) { map.removeLayer(l); });
    lines = [];
    const pts = stopMarkers.map(function (mk) { return mk.getLatLng(); });
    for (let i = 0; i < pts.length - 1; i++) {
      const seg = L.polyline([pts[i], pts[i + 1]], {
        color: '#1462ff', weight: 4, opacity: 0.7, className: 'route-seg'
      }).addTo(map);
      seg.on('mouseover', function () { seg.setStyle({ weight: 7, opacity: 0.95 }); });
      seg.on('mouseout', function () { seg.setStyle({ weight: 4, opacity: 0.7 }); });
      seg.on('click', function (e) {            // insert a stop between i and i+1
        L.DomEvent.stopPropagation(e);
        layerClickAt = Date.now();
        stops.splice(i + 1, 0, { lat: e.latlng.lat, lon: e.latlng.lng });
        if (selectedIndex !== null && selectedIndex >= i + 1) selectedIndex++;
        redrawRoute();
      });
      lines.push(seg);
    }
    updateInfo();
  }
  function redrawRoute() {
    stopMarkers.forEach(function (mk) { map.removeLayer(mk); });
    stopMarkers = stops.map(function (s, i) {
      const mk = L.marker([s.lat, s.lon], {
        draggable: true, icon: stopIcon(i + 1, i === selectedIndex)
      }).addTo(map);
      mk.on('click', function (e) {            // click selects (toggles) this stop
        L.DomEvent.stopPropagation(e);
        layerClickAt = Date.now();
        selectedIndex = (selectedIndex === i) ? null : i;
        redrawRoute();
      });
      mk.on('drag', updateLine);               // line follows while dragging
      mk.on('dragend', function () {
        const p = mk.getLatLng();
        stops[i] = { lat: p.lat, lon: p.lng };  // commit the new position
        updateLine();
      });
      mk.on('contextmenu', function (e) {       // right-click removes this stop
        L.DomEvent.preventDefault(e);
        L.DomEvent.stopPropagation(e);
        removeStop(i);
      });
      return mk;
    });
    updateLine();
    document.getElementById('stops').textContent = 'stops: ' + stops.length
      + (selectedIndex !== null ? ' (#' + (selectedIndex + 1) + ' selected)' : '');
  }

  map.on('click', function (e) {
    if (Date.now() - layerClickAt < 150) return;  // click landed on a stop or line
    if (mode === 'set') {
      device.setLatLng(e.latlng);
      teleport(e.latlng.lat, e.latlng.lng);
    } else {
      const stop = { lat: e.latlng.lat, lon: e.latlng.lng };
      if (selectedIndex !== null) {
        // insert before the selected stop; the new stop lands at selectedIndex
        // and stays selected, so repeated clicks keep building before it
        stops.splice(selectedIndex, 0, stop);
      } else {
        stops.push(stop);                       // nothing selected: add at the end
      }
      redrawRoute();
    }
  });
  device.on('click', function (e) {            // don't let device clicks add a stop
    L.DomEvent.stopPropagation(e);
    layerClickAt = Date.now();
  });
  device.on('dragend', function () {
    const p = device.getLatLng();
    teleport(p.lat, p.lng);
  });

  document.querySelectorAll('input[name=mode]').forEach(function (el) {
    el.addEventListener('change', function () { mode = el.value; });
  });
  document.getElementById('follow').addEventListener('change', function () {
    if (this.checked) map.panTo(device.getLatLng());   // center now when enabled
  });
  document.getElementById('realistic').addEventListener('change', function () {
    realismOpts.style.display = this.checked ? '' : 'none';   // reveal the knobs when on
  });
  let speedTimer = null;
  document.getElementById('speed').addEventListener('input', function () {
    updateInfo();                                   // instant length/time readout
    clearTimeout(speedTimer);
    speedTimer = setTimeout(function () {            // apply to a running drive
      const s = curSpeed();
      if (s > 0) post('/speed', { speed: s }).catch(function () {});
    }, 300);
  });
  document.getElementById('undo').addEventListener('click', function () {
    stops.pop();
    if (selectedIndex !== null && selectedIndex >= stops.length) selectedIndex = null;
    redrawRoute();
  });
  document.getElementById('clearBtn').addEventListener('click', function () {
    stops = []; selectedIndex = null; redrawRoute();
  });
  document.addEventListener('keydown', function (e) {
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT')) return;
    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (selectedIndex !== null) { e.preventDefault(); removeStop(selectedIndex); }
    } else if (e.key === 'Escape') {
      if (selectedIndex !== null) { selectedIndex = null; redrawRoute(); }
    }
  });
  const playStop = document.getElementById('playStop');
  const pauseBtn = document.getElementById('pauseBtn');
  function updateTransport(dr, pa) {
    driving = dr; paused = pa;
    if (dr) {
      playStop.innerHTML = '⏹ Stop';
      pauseBtn.disabled = false;
      pauseBtn.innerHTML = pa ? '▶ Resume' : '⏸ Pause';
    } else {
      playStop.innerHTML = '▶ Drive';
      pauseBtn.disabled = true;
      pauseBtn.innerHTML = '⏸ Pause';
    }
  }
  playStop.addEventListener('click', async function () {
    if (!driving) {
      if (stops.length < 2) { show('add at least 2 stops in Route mode', 'err'); return; }
      show('starting drive...', 'pending');
      try {
        await post('/route', {
          points: stops.map(function (s) { return [s.lat, s.lon]; }),
          speed: curSpeed() || 30,
          repeat: document.getElementById('repeat').value,
          realistic: document.getElementById('realistic').checked,
          realism: realismParams()
        });
        updateTransport(true, false);     // optimistic; poller reconciles
      } catch (e) { show('error: ' + e.message, 'err'); }
    } else {
      try { await post('/stop', {}); updateTransport(false, false); show('stopped', 'ok'); }
      catch (e) { show('error: ' + e.message, 'err'); }
    }
  });
  pauseBtn.addEventListener('click', async function () {
    if (!driving) return;
    try {
      const r = await post('/pause', {});
      updateTransport(true, !!r.paused);
      show(r.paused ? 'paused' : 'resumed', r.paused ? 'pending' : 'ok');
    } catch (e) { show('error: ' + e.message, 'err'); }
  });

  // ---- saved routes (load / save / delete) ----
  let savedRoutes = {};
  function setMode(m) {
    mode = m;
    const radio = document.querySelector('input[name=mode][value=' + m + ']');
    if (radio) radio.checked = true;
  }
  async function refreshRoutes() {
    try {
      const r = await fetch('/routes');
      if (!r.ok) return;
      savedRoutes = await r.json();
      const sel = document.getElementById('routeSel');
      const cur = sel.value;
      sel.innerHTML = '<option value="">saved routes...</option>';
      Object.keys(savedRoutes).sort().forEach(function (n) {
        const o = document.createElement('option');
        o.value = n; o.textContent = n;
        sel.appendChild(o);
      });
      if (savedRoutes[cur]) sel.value = cur;
    } catch (e) {}
  }
  document.getElementById('loadBtn').addEventListener('click', function () {
    const n = document.getElementById('routeSel').value;
    if (!n || !savedRoutes[n]) { show('pick a saved route to load', 'err'); return; }
    const r = savedRoutes[n];
    stops = (r.points || []).map(function (p) { return { lat: p[0], lon: p[1] }; });
    selectedIndex = null;
    document.getElementById('speed').value = r.speed || 30;
    document.getElementById('repeat').value = r.repeat || 'once';
    document.getElementById('routeName').value = n;
    setMode('route');
    redrawRoute();
    if (stops.length) {
      map.fitBounds(stops.map(function (s) { return [s.lat, s.lon]; }), { padding: [40, 40] });
    }
    show('loaded "' + n + '" (' + stops.length + ' stops)', 'ok');
  });
  document.getElementById('saveBtn').addEventListener('click', async function () {
    const n = document.getElementById('routeName').value.trim();
    if (!n) { show('enter a name to save', 'err'); return; }
    if (stops.length < 2) { show('add at least 2 stops to save', 'err'); return; }
    try {
      await post('/routes/save', {
        name: n,
        points: stops.map(function (s) { return [s.lat, s.lon]; }),
        speed: Number(document.getElementById('speed').value) || 30,
        repeat: document.getElementById('repeat').value
      });
      await refreshRoutes();
      document.getElementById('routeSel').value = n;
      show('saved "' + n + '"', 'ok');
    } catch (e) { show('error: ' + e.message, 'err'); }
  });
  document.getElementById('delBtn').addEventListener('click', async function () {
    const n = document.getElementById('routeSel').value;
    if (!n) { show('pick a saved route to delete', 'err'); return; }
    try {
      await post('/routes/delete', { name: n });
      await refreshRoutes();
      show('deleted "' + n + '"', 'ok');
    } catch (e) { show('error: ' + e.message, 'err'); }
  });
  refreshRoutes();
  updateInfo();

  // Follow the device while it drives a route.
  setInterval(async function () {
    try {
      const r = await fetch('/state');
      if (!r.ok) return;
      const st = await r.json();
      const wasDriving = driving;
      updateTransport(st.driving, !!st.paused);
      if (st.driving) {
        if (!wasDriving) lastCarPos = null;       // fresh heading at drive start
        updateMarker({ lat: st.lat, lon: st.lon }, st.speed, st.course);   // standing / walking / driving + heading
        updateHud(st.speed, wasDriving);          // live speed + acceleration (natural mode only)
        device.setLatLng([st.lat, st.lon]);
        if (document.getElementById('follow').checked) map.panTo([st.lat, st.lon]);
        show((st.paused ? 'paused: ' : 'driving: ') + fmt(st.lat, st.lon),
          st.paused ? 'pending' : 'ok');
      } else if (wasDriving) {                    // just stopped -> hold position, standing
        lastCarPos = { lat: st.lat, lon: st.lon };
        setKind('stand');
        liveHud.style.display = 'none'; lastSpeed = null;   // clear the live readout
        device.setLatLng([st.lat, st.lon]);
        show('spoofing: ' + fmt(st.lat, st.lon), 'ok');
      }
    } catch (e) {}
  }, 1000);
</script>
</body>
</html>"""


class _MapRequestHandler(BaseHTTPRequestHandler):
    """Serves the map page and drives the device from the browser.

    The owning server carries the shared bits the handler needs:
      server.html        - the substituted MAP_HTML string
      server.loop        - the asyncio loop running LocationSimulation
      server.current     - {lat, lon, active, driving} for GET /state
      server.apply_set   - coroutine fn (lat, lon) -> teleport
      server.start_route - coroutine fn (points, speed_mps, repeat, realistic, realism) -> drive
      server.stop_drive  - coroutine fn () -> halt any active drive
    """

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass

    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _run(self, coro):
        """Run a control coroutine on the asyncio loop and wait for it."""
        return asyncio.run_coroutine_threadsafe(coro, self.server.loop).result(
            timeout=20
        )

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, self.server.html, "text/html; charset=utf-8")
        elif self.path == "/state":
            state = dict(self.server.current)
            state["paused"] = getattr(self.server, "paused", False)
            self._send(200, json.dumps(state))
        elif self.path == "/routes":
            self._send(200, json.dumps(load_routes()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self) -> None:
        if self.path == "/set":
            self._handle_set()
        elif self.path == "/route":
            self._handle_route()
        elif self.path == "/stop":
            self._handle_stop()
        elif self.path == "/speed":
            self._handle_speed()
        elif self.path == "/pause":
            self._handle_pause()
        elif self.path == "/routes/save":
            self._handle_save_route()
        elif self.path == "/routes/delete":
            self._handle_delete_route()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _handle_set(self) -> None:
        try:
            p = self._read_json()
            lat, lon = float(p["lat"]), float(p["lon"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "expected JSON {lat, lon}"}))
            return
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            self._send(400, json.dumps({"error": "lat/lon out of range"}))
            return
        try:
            self._run(self.server.apply_set(lat, lon))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(200, json.dumps({"ok": True}))

    def _handle_route(self) -> None:
        try:
            p = self._read_json()
            pts = [(float(a), float(b)) for a, b in p["points"]]
            speed = float(p.get("speed", DEFAULT_ROUTE_SPEED_MPH))
            repeat = str(p.get("repeat", "once"))
            realistic = bool(p.get("realistic", False))
            realism = (_realism_params_from_json(p.get("realism") or {})
                       if realistic else None)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps(
                {"error": "expected {points:[[lat,lon],...], speed, repeat}"}))
            return
        if len(pts) < 2:
            self._send(400, json.dumps({"error": "need at least 2 stops"}))
            return
        if any(not (-90.0 <= a <= 90.0) or not (-180.0 <= b <= 180.0)
               for a, b in pts):
            self._send(400, json.dumps({"error": "a stop is out of range"}))
            return
        if speed <= 0:
            self._send(400, json.dumps({"error": "speed must be positive"}))
            return
        if repeat not in ("once", "loop", "bounce"):
            self._send(400, json.dumps({"error": "repeat must be once|loop|bounce"}))
            return
        try:
            self._run(self.server.start_route(
                pts, speed * MPH_TO_MPS, repeat, realistic, realism))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(200, json.dumps({"ok": True}))

    def _handle_stop(self) -> None:
        try:
            self._run(self.server.stop_drive())
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(200, json.dumps({"ok": True}))

    def _handle_speed(self) -> None:
        try:
            speed = float(self._read_json()["speed"])  # mph
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "expected JSON {speed}"}))
            return
        if speed <= 0:
            self._send(400, json.dumps({"error": "speed must be positive"}))
            return
        # Plain attribute write picked up by the running drive's get_speed.
        self.server.speed_mps = speed * MPH_TO_MPS
        self._send(200, json.dumps({"ok": True}))

    def _handle_pause(self) -> None:
        # Toggle pause only while a drive is active; the running drive's
        # get_speed returns 0 while paused, so it holds position.
        task = getattr(self.server, "drive_task", None)
        if task is None or task.done():
            self.server.paused = False
        else:
            self.server.paused = not getattr(self.server, "paused", False)
        self._send(200, json.dumps({"paused": self.server.paused}))

    def _handle_save_route(self) -> None:
        try:
            p = self._read_json()
            name = str(p["name"]).strip()
            pts = [[float(a), float(b)] for a, b in p["points"]]
            speed = float(p.get("speed", DEFAULT_ROUTE_SPEED_MPH))
            repeat = str(p.get("repeat", "once"))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps(
                {"error": "expected {name, points:[[lat,lon],...], speed, repeat}"}))
            return
        if not name:
            self._send(400, json.dumps({"error": "name is required"}))
            return
        if len(pts) < 2:
            self._send(400, json.dumps({"error": "need at least 2 stops"}))
            return
        if any(not (-90.0 <= a <= 90.0) or not (-180.0 <= b <= 180.0)
               for a, b in pts):
            self._send(400, json.dumps({"error": "a stop is out of range"}))
            return
        if speed <= 0 or repeat not in ("once", "loop", "bounce"):
            self._send(400, json.dumps({"error": "bad speed or repeat"}))
            return
        try:
            save_route(name, pts, speed, repeat)
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(200, json.dumps({"ok": True}))

    def _handle_delete_route(self) -> None:
        try:
            name = str(self._read_json()["name"]).strip()
        except (KeyError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "expected {name}"}))
            return
        if delete_route(name):
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "no such route"}))


async def cmd_map(args: argparse.Namespace) -> int:
    _check_privileged_or_tunneld("map")

    iphone = await select_iphone(args.udid)
    print(
        f"connected: {iphone['device_name']} "
        f"({iphone['product_type']}) iOS {iphone['product_version']}"
    )
    print(f"udid:      {iphone['udid']}")

    from pymobiledevice3.services.dvt.instruments.location_simulation import (
        LocationSimulation,
    )

    # Center on the last known fix if we have one, else a sensible default.
    state = read_state()
    if state and isinstance(state.get("lat"), (int, float)) \
            and isinstance(state.get("lon"), (int, float)):
        center_lat, center_lon = float(state["lat"]), float(state["lon"])
    else:
        center_lat, center_lon = MAP_DEFAULT_CENTER
    car_img, car_size, car_anchor = car_icon_html()
    walk_img, walk_size, walk_anchor = _img_icon_html("walking.png", BADGE_DISPLAY_MAX_PX)
    stand_img, stand_size, stand_anchor = _img_icon_html("standing.png", BADGE_DISPLAY_MAX_PX)
    html = (MAP_HTML
            .replace("__LAT__", repr(center_lat))
            .replace("__LON__", repr(center_lon))
            .replace("__ZOOM__", str(MAP_DEFAULT_ZOOM))
            .replace("__ACTIVE__", "false")
            .replace("__CAR_SIZE__", car_size)
            .replace("__CAR_ANCHOR__", car_anchor)
            .replace("__CAR_IMG__", car_img)
            .replace("__WALK_SIZE__", walk_size)
            .replace("__WALK_ANCHOR__", walk_anchor)
            .replace("__WALK_IMG__", walk_img)
            .replace("__STAND_SIZE__", stand_size)
            .replace("__STAND_ANCHOR__", stand_anchor)
            .replace("__STAND_IMG__", stand_img))

    async with open_dvt(iphone["udid"]) as dvt:
        async with LocationSimulation(dvt) as sim:
            loop = asyncio.get_running_loop()
            control = asyncio.Lock()  # serializes all device-control ops

            server = ThreadingHTTPServer(("127.0.0.1", 0), _MapRequestHandler)
            server.html = html
            server.loop = loop
            server.current = {"lat": center_lat, "lon": center_lon,
                              "active": False, "driving": False}
            server.drive_task = None
            # Live speed (m/s) the active drive reads each tick; POST /speed
            # updates it so a change takes hold without restarting the drive.
            server.speed_mps = DEFAULT_ROUTE_SPEED_MPH * MPH_TO_MPS
            # While paused the drive holds position (get_speed returns 0).
            server.paused = False

            def set_current(lat, lon, *, active=True, driving=False,
                            course=None, speed=None) -> None:
                # course (deg) / speed (m/s) are only set during a realistic
                # drive; the page uses them for the marker heading/kind and
                # falls back to position deltas when they're absent.
                server.current = {"lat": lat, "lon": lon,
                                  "active": active, "driving": driving,
                                  "course": course, "speed": speed}

            async def cancel_drive() -> None:
                task = server.drive_task
                server.drive_task = None
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            def record_state(name, lat, lon) -> None:
                write_state({
                    "udid": iphone["udid"],
                    "device_name": iphone["device_name"],
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "pid": os.getpid(),
                })

            async def apply_set(lat, lon) -> None:
                async with control:
                    await cancel_drive()
                    server.paused = False
                    await sim.set(lat, lon)
                    set_current(lat, lon, active=True, driving=False)
                    record_state(f"{lat}, {lon}", lat, lon)
                    _progress(f"set {lat:.6f}, {lon:.6f}")

            async def start_route(pts, speed_mps, repeat, realistic=False,
                                  realism=None) -> None:
                named = [(f"{la}, {lo}", la, lo) for la, lo in pts]
                motion = MotionState(**(realism or {})) if realistic else None
                async with control:
                    await cancel_drive()
                    server.speed_mps = speed_mps  # seed live speed for this run
                    server.paused = False

                    def on_update(la, lo, course=None, speed=None):
                        set_current(la, lo, active=True, driving=True,
                                    course=course, speed=speed)

                    async def runner():
                        await drive_repeated(
                            sim, named, speed_mps, repeat,
                            on_update=on_update, show_progress=False,
                            get_speed=lambda: 0.0 if server.paused
                            else server.speed_mps,
                            motion=motion,
                        )
                        # `once` finished: hold at the final stop.
                        server.paused = False
                        set_current(named[-1][1], named[-1][2],
                                    active=True, driving=False)
                        _progress("route complete; holding at final stop")

                    server.drive_task = asyncio.create_task(runner())
                    label = " -> ".join(p[0] for p in named)
                    record_state(label, named[0][1], named[0][2])
                    _progress(f"driving {len(named)} stops @ "
                              f"{speed_mps / MPH_TO_MPS:.0f} mph ({repeat}"
                              f"{', natural' if realistic else ''})")

            async def stop_drive() -> None:
                async with control:
                    await cancel_drive()
                    server.paused = False
                    cur = server.current
                    set_current(cur["lat"], cur["lon"],
                                active=cur.get("active", True), driving=False)
                    _progress("drive stopped (holding)")

            server.apply_set = apply_set
            server.start_route = start_route
            server.stop_drive = stop_drive

            url = f"http://127.0.0.1:{server.server_address[1]}/"
            thread = threading.Thread(
                target=server.serve_forever, name="gpsspoof-map", daemon=True
            )
            thread.start()

            print()
            print(f"  {ANSI['bold']}map ready{ANSI['reset']}  ->  {url}")
            print(f"  device     ->  {iphone['device_name']} "
                  f"[{iphone['udid']}]")
            print(f"  {ANSI['dim']}click to set a point, or switch to Route "
                  f"mode to build and Drive a route; Ctrl-C to clear and exit"
                  f"{ANSI['reset']}")
            print()
            try:
                webbrowser.open(url)
            except Exception:
                print(f"  {ANSI['yellow']}could not auto-open a browser; "
                      f"open the URL above manually{ANSI['reset']}")

            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass

            try:
                await stop.wait()
            finally:
                print("\n... shutting down map server, clearing location...")
                await cancel_drive()
                server.shutdown()
                server.server_close()
                try:
                    await sim.clear()
                    print("... cleared. real GPS resumed.")
                except Exception as e:
                    print(f"warning: clear failed: {e}", file=sys.stderr)
                write_state(None)
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
            "  gpsspoof map                      # click-on-a-map browser UI\n"
            "  gpsspoof list                     # show known locations\n"
            "  gpsspoof set seattle              # start spoofing (Ctrl-C to stop)\n"
            "  gpsspoof set 47.490308 -122.205647   # spoof to raw coordinates\n"
            "  gpsspoof route kent seattle redmond --speed 30   # drive a route\n"
            "  gpsspoof route kent seattle redmond --bounce     # there-and-back\n"
            "  gpsspoof route kent seattle redmond --realistic  # human-like motion + jitter\n"
            "  gpsspoof route kent seattle --save commute       # save + drive\n"
            "  gpsspoof routes                   # list saved routes\n"
            "  gpsspoof route --load commute --loop   # drive a saved route\n"
            "  gpsspoof clear                    # stop any active spoof\n"
            "  gpsspoof status                   # show current state\n"
            "  gpsspoof add airport 47.4502 -122.3088\n"
            "  gpsspoof rm airport\n"
            "\n"
            "Note: `ui`, `map`, `set`, `route`, `clear` need either root\n"
            "(`sudo gpsspoof ...`) or a running tunneld daemon.\n"
            "See README: 'Skip sudo with tunneld'."
        ),
    )
    p.add_argument(
        "--udid",
        help="select a specific iPhone by UDID when more than one is connected",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("ui", help="interactive menu mode (needs sudo or tunneld)")
    sub.add_parser(
        "map",
        help="open a clickable browser map to set the location "
             "(needs sudo or tunneld)",
    )
    sub.add_parser("list", help="list available named locations")

    p_set = sub.add_parser(
        "set",
        help="start spoofing to a named location or raw coordinates "
             "(needs sudo or tunneld)",
    )
    p_set.add_argument(
        "target",
        nargs="+",
        metavar="LOCATION | LAT LON",
        help="a name from locations.json (e.g. seattle), or raw coordinates "
             "(e.g. 47.490308 -122.205647)",
    )

    p_route = sub.add_parser(
        "route",
        help="drive a route of waypoints, or a saved route, at a speed "
             "(needs sudo or tunneld)",
    )
    p_route.add_argument(
        "waypoints",
        nargs="*",
        metavar="WAYPOINT",
        help="two or more waypoints, each a name (e.g. seattle) or a single "
             "lat,lon token (e.g. 47.5049,-122.2333). For a southern/western "
             "point whose token starts with '-', put '--' before the list. "
             "Omit when using --load.",
    )
    p_route.add_argument(
        "--speed",
        default=None,
        help=f"travel speed; a bare number is mph (default "
             f"{DEFAULT_ROUTE_SPEED_MPH}). Units allowed: e.g. 30mph, "
             f"48km/h, 13m/s.",
    )
    p_route.add_argument(
        "--save", metavar="NAME",
        help="save this route under NAME (to routes.json) before driving",
    )
    p_route.add_argument(
        "--load", metavar="NAME",
        help="drive a previously saved route by NAME (see `gpsspoof routes`)",
    )
    p_route.add_argument(
        "--delete", metavar="NAME",
        help="delete the saved route NAME and exit (no device needed)",
    )
    p_route.add_argument(
        "--realistic", "--natural",
        dest="realistic", action="store_true",
        help="drive like a human: variable speed, real acceleration/braking, "
             "slowing for corners, and drifting GPS jitter (simulates a "
             "varying accuracy). Off by default (exact, constant-speed motion).",
    )
    p_route.set_defaults(repeat=None)
    repeat_mode = p_route.add_mutually_exclusive_group()
    repeat_mode.add_argument(
        "--loop",
        dest="repeat", action="store_const", const="loop",
        help="repeat as a closed loop (...last -> first -> second...) "
             "until Ctrl-C",
    )
    repeat_mode.add_argument(
        "--bounce",
        dest="repeat", action="store_const", const="bounce",
        help="repeat back and forth, reversing at each end "
             "(...second-last -> last -> second-last...) until Ctrl-C",
    )

    sub.add_parser("routes", help="list saved routes (from routes.json)")

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
    if args.cmd == "routes":
        return cmd_routes(args)
    if args.cmd == "set":
        try:
            return asyncio.run(cmd_set(args))
        except KeyboardInterrupt:
            return 130
    if args.cmd == "route":
        try:
            return asyncio.run(cmd_route(args))
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
    if args.cmd == "map":
        try:
            return asyncio.run(cmd_map(args))
        except KeyboardInterrupt:
            return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
