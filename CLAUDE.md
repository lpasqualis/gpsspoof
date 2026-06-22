# CLAUDE.md

Guidance for working in this repo. See `README.md` for full user docs.

## What this is

`gpsspoof` — a macOS tool that spoofs the GPS location of a USB-connected
iPhone (iOS 17+) via [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).
It can set a fixed point, drive a route through waypoints at a speed
(once / loop / bounce), and serve a localhost browser map for clicking a
point or building and driving routes live.

## Layout

- `iphone_spoof.py` — the entire implementation (one module). The top
  docstring documents the device-tunnel chain and command model; read it
  first.
- `pyproject.toml` — packaging; entry point `gpsspoof = iphone_spoof:main`.
- `scripts/install-tunneld.sh`, `scripts/uninstall-tunneld.sh` — set up
  the optional tunneld launchd daemon (lets device commands run without
  sudo).
- `.venv/` — local virtualenv with `pymobiledevice3` installed.

## Run / dev

- Run from the venv: `.venv/bin/python -m iphone_spoof <args>`, or the
  installed `gpsspoof` entry point.
- Compile-check after edits: `python -m py_compile iphone_spoof.py`.
- No automated test suite. For non-device logic (coordinate/route/speed
  parsing, the route engine with a fake `sim`, the map HTTP handler over a
  real loopback socket), write a short throwaway script under `tmp/`, run
  it, and delete it when done — keep temp files in the project, never
  `/tmp`.

## Device access requires privilege

`set`, `route`, `map`, `ui`, `clear` reach the device and need **either**
root (`sudo gpsspoof ...`) **or** a running tunneld daemon (`open_rsd`
tries tunneld first, then falls back to building an in-process tunnel,
which needs root). `list`, `status`, `add`, `rm`, `routes` are
unprivileged (local JSON + usbmuxd only).

Many code paths can only be exercised with a real iPhone connected and
unlocked. When no device is attached, device commands fail fast with
`no iPhone connected over USB` — that's expected, not a bug.

## Key pieces

- Tunnel setup: `open_rsd` / `open_dvt` (tunneld vs in-process).
- Movement: `drive_route` interpolates between waypoints and integrates
  distance from real elapsed time × the current speed; `_update_interval`
  paces updates adaptively (~100 ft/step, clamped to 0.1–1.0 s);
  `drive_repeated` adds once/loop/bounce.
- Realistic motion (opt-in): pass a `MotionState` into `drive_route`/
  `drive_repeated` (`route --realistic`, map "natural motion" checkbox, or
  the `natural` keyword in `ui`). It makes the commanded speed a wandering
  cruise target, accel/decel-limits every change, brakes for corners (and
  to a stop on `once` arrival via `stop_at_end`), and adds drifting GPS
  jitter. State persists across segments/passes so motion is continuous.
  The protocol sends only lat/lon — `horizontalAccuracy` isn't settable —
  so jitter stands in for variable accuracy (added to the clean path point,
  never the previous fix, and bounded to the current accuracy radius, so it
  can't accumulate). The key tunables
  (speed_variation, accel_max, decel_max, jitter_m, jitter_max_m) are
  `MotionState` constructor args defaulting to the module-level `REALISM_*`
  constants; the map UI overrides them per drive (knobs revealed by the
  checkbox, sent in the `/route` body, validated by
  `_realism_params_from_json`), while `route --realistic`/`ui` use defaults.
  In realistic mode `on_update`/`/state` also carry the clean `course`+`speed`
  so the map marker doesn't spin from jitter and the page can show a live
  speed/acceleration (g) HUD (acceleration is computed client-side from
  successive speed samples).
- Map server: `cmd_map` runs a stdlib `ThreadingHTTPServer` alongside the
  asyncio `LocationSimulation`; `_MapRequestHandler` bridges HTTP →
  asyncio via `run_coroutine_threadsafe`. The served page is the
  `MAP_HTML` string (Leaflet + OpenStreetMap, no API key); tokens like
  `__LAT__` are substituted at serve time. Endpoints: `/set`, `/route`,
  `/stop`, `/speed`, `/pause`, `/state`, `/routes`, `/routes/save`,
  `/routes/delete`.
- Config under `~/.config/iphone-spoof/`: `locations.json` (named
  points), `routes.json` (saved routes; shared by CLI and map),
  `state.json` (current session, for `status`). `get_config_dir()`
  resolves `SUDO_USER`'s home so `sudo` and unprivileged runs agree.

## Conventions

- Keep everything in the single module; match the existing style and the
  comment density around the code you touch. No emojis in code or output.
- Update `README.md` (and this file) when commands or behavior change.
- Commit style: semantic messages (`feat:`, `fix:`, `docs:`...), atomic
  where practical. This is a solo repo that commits directly to `main`.
