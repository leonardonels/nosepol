# NoSePol — Trieste live buses POC (lines 6, 8, 9)

A backend polls TPL FVG's realtime API, fits each bus's position along its
route, and serves JSON. A Leaflet map in `web/` shows the result.

```
python3 tools/scan_stops.py      # one-off: builds data/stops.json (~35 min)
python3 tools/fill_stops.py      # fills codes for route stops the scan missed
python3 server/app.py            # http://localhost:8080
python3 server/app.py --lines T06,T08,T09,T17 --port 8080
```

Python 3.10+, stdlib only. The page loads Leaflet and OSM tiles from their CDNs.

## What the upstream API gives us

Base `https://realtime.tplfvg.it/API/v1.0/polemonitor` (the backend of
realtime.tplfvg.it, the official stop-monitor page):

| endpoint | returns |
|---|---|
| `info?StopCode=10001` | stop name + coordinates |
| `mrcruns?StopCode=10001&IsUrban=true` | the **next** run per line/direction at that stop, with `Race` (trip id), `Vehicle`, and for started runs the bus's **live GPS** (`Latitude/Longitude`) |
| `LineGeoTrack?Line=T06&Race=745` | the trip's shape as a list of stop-to-stop segments |

Findings that shape the design:
- GPS is refreshed for all buses in one batch about every 20–25 s.
- A stop lists only the next bus per line and direction, so to see every bus
  we poll every 4th stop of each route plus the terminus (`STOP_STRIDE`).
- There's no list-stops endpoint, so `tools/scan_stops.py` walks the codes
  (`<zone><4 digits>`) once.
- Short-turn trips show up as e.g. `T06/` and are grouped under `T06`.

## How the server works (`server/app.py`)

1. **Discovery**: it walks stops outward from the centre until each line has a
   variant in both directions. Then it fetches `LineGeoTrack` for every new trip it
   sees and caches the shape in `data/routes.json`. Segment endpoints are matched
   to stop codes.
2. **Batch-synced polling**: one heartbeat stop is polled every 5 s. When its
   GPS values change, a new batch has landed, so the server sweeps the poll set once
   (4 threads). That's about one sweep per GPS refresh, timestamped within ~5 s.
3. **Estimator** (`server/estimator.py`): each fix is projected onto the trip's
   shape to get `s`, the metres along the route. Projection prefers forward motion,
   so loops and out-and-back routes don't jump. Over a 150 s window it fits
   `s(t) = s0 + v·t` by least squares (v ≥ 0). It extrapolates at most 60 s past
   the last fix and reports the RMS residual.

## API

- `GET /api/lines`: lines with display `name`, `color`, `ink` (text colour on it), route
  variants and vehicle counts. Colours follow the order of `--lines`, so adding a line
  needs no page change.
- `GET /api/vehicles` or `/api/lines/T06/vehicles`: one entry per trip, with
  `trip, vehicle, s, length, lat, lon, v (m/s), residual (m), n_fixes, age (s), stopped_s, stop_kind, gps{lat,lon,offroute}, next_stop{name,dist,eta_s}, api_arrival`.
  A bus whose GPS stops changing is `stopped_s` seconds still, with `stop_kind`
  `terminus` (waiting, kept up to 15 min) or `midroute` (may have a problem, kept up to 1 h).
- `GET /api/shapes` or `/api/lines/T06/shapes`: polylines and stops with `s`
- `GET /api/stops?q=…&near=lat,lon&limit=20`: stops on tracked routes, matched by any
  words of the name or code (accents and case ignored) and/or sorted by distance
- `GET /api/stops/{code}`: every tracked bus still heading to the stop, with our ETA
  (`eta_s`), the operator's (`operator`, fetched live and cached 20 s) and later
  timetable passes. Untracked runs the operator lists appear with `live: false`.
- `GET /api/trips/{trip}`: one bus, the stops ahead with ETAs, and its recent fixes
- `GET /api/status`: last GPS batch, batch interval, stale/stopped counts, stops with
  no code (a hint that a resync is due)
- `GET /api/debug`: poll set, batch intervals, raw fix history

The page uses hash links that can be shared: `/#/stop/09001`, `/#/bus/T06:1086`.

## Keeping data fresh (resync)

`data/stops.json` and `data/routes.json` are snapshots scraped from the API,
which has no stop list and no change feed. Stops, codes and route variants can
change with timetable updates, detours or seasonal service. Resync when stops
show as "code unknown", buses go missing, or after a timetable change:

```
python3 tools/scan_stops.py    # full rescan (~35 min)
python3 tools/fill_stops.py    # probe gaps until every route stop has a code
rm data/routes.json            # refetch route shapes on next start
```

Automating this (e.g. a weekly job) is worth doing once this outgrows the POC.

## Running on a server (Docker)

On an always-on host (e.g. `roxan`):

```bash
git clone git@github.com:leonardonels/nosepol.git && cd nosepol
docker compose up -d --build
```

The map is then at `http://<host>:8080`. Options via environment / `.env`:
`NOSEPOL_PORT` (host port, default 8080) and `NOSEPOL_LINES` (default `T06,T08,T09`).

- Runs as a non-root user with a healthcheck on `/api/lines`. No restart policy: after a
  crash or a host reboot it stays stopped until you run `docker compose up -d` again.
- `data/` lives in the `nosepol-data` volume, seeded from the image on first run, so the
  learned routes cache survives rebuilds and the git checkout stays clean.
- A bind mount works too (`/srv/nosepol:/app/data`) and may start empty: at startup any
  missing `stops.json` / `routes.json` is copied from the copy bundled in the image
  (`/app/seed`). Existing files are never overwritten; delete one and restart to reset it.
  The folder must be writable by UID 1000.
- Update code: `git pull && docker compose up -d --build`.
- After a stop-DB resync (new `data/stops.json` in git) the volume must be reseeded:
  `docker compose down -v && docker compose up -d --build`.
- To install the PWA / use it away from home, put it behind HTTPS, e.g. `tailscale serve 8080`
  or a Cloudflare Tunnel.

## Next steps
- Validate `STOP_STRIDE` coverage against the timetable (are buses missing?).
- Better ETA: per-segment historical speeds instead of the instantaneous `v`.
- Android thin client: widget, Wear OS tile, "leave now" notification.
