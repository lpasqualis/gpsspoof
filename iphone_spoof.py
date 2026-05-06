"""iphone-spoof: spoof GPS on a USB-connected iPhone (iOS 17+) via pymobiledevice3.

CLI entry point: `spoof` (defined in pyproject.toml). Targets pymobiledevice3 9.x.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional


DEFAULT_LOCATIONS = {
    # Washington
    "seattle":   {"lat": 47.6062, "lon": -122.3321},
    "renton":    {"lat": 47.4829, "lon": -122.2171},
    "kent":      {"lat": 47.3809, "lon": -122.2348},
    "issaquah":  {"lat": 47.5301, "lon": -122.0326},
    "bellevue":  {"lat": 47.6101, "lon": -122.2015},
    "redmond":   {"lat": 47.6740, "lon": -122.1215},
    "olympia":   {"lat": 47.0379, "lon": -122.9007},
    "tacoma":    {"lat": 47.2529, "lon": -122.4443},
    # Other US
    "vegas":     {"lat": 36.1699, "lon": -115.1398},
    "portland":  {"lat": 45.5152, "lon": -122.6784},
    "nyc":       {"lat": 40.7128, "lon":  -74.0060},
    "la":        {"lat": 34.0522, "lon": -118.2437},
}


def get_config_dir() -> Path:
    # Under sudo, prefer the invoking user's home so the same locations.json
    # is read whether or not sudo is in front of the command.
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
    """Return one entry per USB-connected iPhone (deduped on UDID)."""
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


@asynccontextmanager
async def open_rsd(udid: str):
    """Yield a connected RemoteServiceDiscoveryService for the given iPhone.

    iOS 17+ exposes developer services over RemoteXPC. Bringing the tunnel
    up creates a `utun` interface, which on macOS requires root.
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
            "Re-run as: sudo -E spoof ..."
        )

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

    # iOS 18.2+ dropped QUIC; TCP works for both old and new, so use TCP.
    try:
        async with start_tunnel_over_core_device(
            service, protocol=TunnelProtocol.TCP
        ) as tr:
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
async def open_dvt(udid: str):
    """Yield a connected DvtProvider for the given iPhone."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    async with open_rsd(udid) as rsd:
        async with DvtProvider(rsd) as dvt:
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
            write_state({
                "udid": iphone["udid"],
                "device_name": iphone["device_name"],
                "name": args.name,
                "lat": lat,
                "lon": lon,
                "pid": os.getpid(),
            })
            print(f"spoofing to {args.name} ({lat}, {lon})")
            print("ctrl-c to stop")

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
                print("\nclearing spoofed location...")
                try:
                    await sim.clear()
                except Exception as e:
                    print(f"warning: clear failed: {e}", file=sys.stderr)
                write_state(None)
    return 0


async def cmd_clear(args: argparse.Namespace) -> int:
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
            "  gpsspoof list                     # show known locations\n"
            "  sudo gpsspoof set seattle         # start spoofing (Ctrl-C to stop)\n"
            "  sudo gpsspoof clear               # stop any active spoof\n"
            "  gpsspoof status                   # show current state\n"
            "  gpsspoof add airport 47.4502 -122.3088\n"
            "  gpsspoof rm airport\n"
            "\n"
            "Note: `set` and `clear` need root on macOS for the RemoteXPC tunnel."
        ),
    )
    p.add_argument(
        "--udid",
        help="select a specific iPhone by UDID when more than one is connected",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("list", help="list available named locations")

    p_set = sub.add_parser("set", help="start spoofing to a named location (needs sudo)")
    p_set.add_argument("name", help="name from locations.json")

    sub.add_parser("clear", help="stop any active spoof on the device (needs sudo)")
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
