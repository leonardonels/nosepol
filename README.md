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
- A stop lists the next bus per line and direction; when two run close together
  (bunched) it often lists both (seen on lines 8 and 9). Otherwise, between two polled stops
  only the bus nearest the second one is seen: any bus behind it on the same stretch
  is hidden from every polled stop. So the server polls every 4th stop of each route
  (`STOP_STRIDE`), more where that leaves over 1 km unpolled (`POLL_GAP_M`), the
  terminus and the stop before it (a terminus lists its next departure, not buses
  arriving), and the stop each tracked bus last passed, which lists the bus behind it.
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
   The poll set is the fixed stops plus the stop behind each tracked bus, so a
   bunched bus is found one sweep after the bus in front of it (~65 stops a sweep
   for lines 6, 8, 9 instead of 35).
3. **Stop codes**: a route stop with no code in `data/stops.json` can't be polled. The
   server downloads the operator's public stop list (`tplfvg.it/services/geojson/points`,
   one request to the website, not the realtime API) at start and then weekly, or every
   6 h while a route stop has no code. It adds new urban stops within 25 km, moves ones
   that moved, and only counts ones no longer listed. Only while that download fails
   does it probe codes just past nearby districts' known ones (daily). A bus on an end
   stretch with no coded stop can't be reported at all: once its speed puts it past
   the last coded stop it is kept as `blind`, moved on at its speed (dashed on the
   map), until its vehicle starts a newer trip or it would have reached the end and
   waited a layover (`blind_timeout`, at most 45 min).
4. **Estimator** (`server/estimator.py`): each fix is projected onto the trip's
   shape to get `s`, the metres along the route. Projection prefers forward motion,
   so loops and out-and-back routes don't jump. Over a 150 s window it fits
   `s(t) = s0 + v·t` by least squares (v ≥ 0). That `v` averages in the stands at
   stops passed in the window, so it is scaled up to a speed between stops (20 s per
   stop, capped at 50 km/h unless the fit is already faster). Between fixes the bus
   moves at that speed and stands 20 s at every stop it reaches: buses stop at nearly
   every stop, so with no fresh GPS it is likelier there than past it. ETAs count
   the same stands. It extrapolates at most 150 s past the last fix (GPS can skip a
   few batches between buildings; no limit for a `blind` bus) and reports the RMS
   residual. A bus listed with the same position for 90 s counts as stopped.
5. **Learned stretch times** (`History` in `server/estimator.py`): when a bus passes
   two stops in a row, the time between arriving at one and arriving at the next
   (interpolated between fixes, stand included) is learned for that pair of stop
   codes, shared by every line and variant that drives it. It keeps a moving average
   per weekday/Saturday/Sunday and hour, per hour, and overall, and saves to
   `data/speeds.json` (per host, not in git). An ETA drives the rest of the current
   stretch at `v`, then takes each whole stretch's learned time once it has 3 runs,
   scaled by how this bus did on its last stretches against theirs (0.75–1.5×).
   Stretches with no history fall back to `v` plus 20 s per stop. `eta_src` on an
   ETA says which was used. `/api/status` `eta_errors` scores both against real
   arrivals: the mean absolute error of the speed-only ETA and of ours, by how far
   ahead it was made.
6. **Route checks**: shapes are cached per trip in `data/routes.json`. Once a day per
   variant, one running trip's shape is fetched again; a trip whose GPS is off its
   shape for 3 fixes in a row is fetched again too (at most once a day per trip, 20
   such requests an hour). A new path with the same stops replaces the variant; a trip
   now on other stops moves to a new variant, and the old one's other trips are
   fetched again as each next runs. A variant no bus ran on for 21 days is dropped.
   When and where it checked is kept in `data/refresh.json` (not in git).

7. **Timetable filter** (`server/kalman.py`, branch `kalman-timetable`): every stop
   that lists a run also gives `Time`, its scheduled time at that stop, and the run's
   first stop is marked `DepartureFromStop`. Collected per trip they make a timetable
   curve T(s). A stop lists only the next bus, so a bus's times are known up to the bus
   ahead; past that, another trip of the same route fills in, shifted to match. The
   bus is then described by its **delay** d = t − T(s), filtered by a 2-state Kalman
   filter (delay and its rate, the rate fading over ~10 min since timetables have slack).
   The timetable is the motion model and GPS the correction: position is S(t − d), ETA
   is the scheduled time at the stop plus the delay expected by then. Between fixes, or
   with no GPS at all, the bus keeps moving at the timetable's pace and its `sigma`
   grows, instead of following a speed fitted to whichever fixes came in.
   - Measurements: each GPS fix (also "listed again at the same spot", so standing
     makes the delay grow), and **passages**: a stop that listed a run last poll and
     now lists another of the same line and direction instead means it went by. They
     need no GPS, so they are used for buses whose GPS is old, frozen or gone.
   - **GPS frozen / gone**: listed at the same position for 150 s mid-route, or listed
     with no position, the bus moves on by its timetable (`estimated`, dashed on the
     map) rather than standing or vanishing. With a known timetable an unlisted bus is
     kept 6 min instead of 3, and once its timetable ends it waits at the terminus like any finished bus.
   - **Ghosts**: a run listed with no GPS, past its first stop and due (or overdue) at
     a stop listing it, that no known bus is about to take, is out running without GPS.
     It is shown at its timetable position (`ghost`, hollow and dashed), `confirmed` once
     a passage was seen, and becomes a normal bus when its GPS appears.
   - **Fewer requests**: only every `NOSEPOL_FULL_EVERY`-th sweep (default 3) polls the
     full set. The others poll each variant's first stop and the two
     stops just ahead of every known bus and ghost. `1` restores full sweeps.
   - **Blended ETA** (branch `eta-blend`): with fresh GPS the ETA is a weighted average
     of the history ETA and the filter's. The weights depend on the time to the stop,
     per minute up to 30: each method counts by 1 / its mean squared error there
     (inverse-variance weighting), learned from the scored arrivals and saved to
     `data/blend.json`. Close to the stop the bus's own recent pace tends to win, further
     out the timetable plus delay, and which wins changes over the day. So errors are
     kept per day type and hour (`wd08`), per hour and overall, like `speeds.json`; a
     finer level starts from the coarser one's (30 arrivals' worth) and moves to its own
     as it learns. With GPS old, frozen or gone, the filter's alone.
     `eta_src` is then `blend` or `timetable`; `/api/status` adds `blend_mae_s` to
     `eta_errors` and `blend_weights` (this hour's bucket and the history ETA's weight per minute now).
     `eta_spread` gives mean, median and 95th percentile of the absolute error for all
     four (speed, history, timetable, blend) on the same recent arrivals: the mean says
     how good it usually is, the p95 how bad it gets.
   - `NOSEPOL_ESTIMATOR=fit` shows the old estimator. Both are always computed:
     `/api/status` `eta_errors` adds `kalman_mae_s` beside `ours_same_mae_s` (the
     history ETA on the same arrivals), and `timetable` counts passages, ghosts,
     frozen GPS and full/targeted sweeps; `requests_per_hour` shows the API load.

## API

- `GET /api/lines`: lines with display `name`, `color`, `ink` (text colour on it), route
  variants and vehicle counts. Colours follow the order of `--lines`, so adding a line
  needs no page change.
- `GET /api/vehicles` or `/api/lines/T06/vehicles`: one entry per trip, with
  `trip, vehicle, s, length, lat, lon, v (m/s, between stops), residual (m), n_fixes, age (s), stopped_s, stop_kind, blind, gps{lat,lon,offroute}, next_stop{name,dist,eta_s}, api_arrival`.
  A bus whose GPS stops changing is `stopped_s` seconds still, with `stop_kind`
  `terminus` (waiting, kept up to 15 min) or `midroute` (may have a problem, kept up to 1 h).
  "Terminus" means within 150 m of the first stop, or at/past the second-to-last stop
  (buses often wait at the stop before the end). A bus in that last stretch that the
  stops stop listing has finished its trip (the operator drops it at once) and usually
  waits there, then starts the line's next run back (watched on lines 6, 8, 9: waits of
  2-10 min). It stays as `stop_kind` `layover`, held at its last position, with
  `next_run` {trip, destination, scheduled}: the line's next unstarted departure listed at
  that terminus stop, paired first come first served. It goes as soon as its vehicle
  appears on a newer trip, or 15 min after its last listing. A terminus stop with no code
  in `data/stops.json` can't be read, so no `next_run` there until a resync.
  Buses still heading to a terminus get `next_run` too when they'll arrive in time
  (within 5 min of the departure; 10 min once paired, so it doesn't flicker).
  `/api/vehicles` also returns `pending`: departures due in the next 20 min that no
  known bus will take (its bus is out of sight: depot, a missed arrival, a restart),
  with the terminus stop's position. One stays until its bus appears with GPS, a known
  bus takes it, or 10 min after its time. `/api/lines` counts them per line.
- `GET /api/stream`: server-sent events, one every 3 s: the `/api/vehicles` payload
  plus `last_batch` (time of the last GPS batch), `batch_interval` and `variants` (a
  routes version that goes up whenever a route is added, changed or dropped; the page
  reloads routes when it changes). The page uses this instead of
  polling and closes it while hidden. A stream closes after 10 min (the browser
  reconnects); past 200 open streams the server answers 503 and the page polls
  `GET /api/live`, the same payload once.
- `GET /api/shapes` or `/api/lines/T06/shapes`: polylines and stops with `s`
- `GET /api/lines/T06/track`: the line page's data. Per direction, the full route's
  stops and every bus on it placed by `s` along that route (short-turn trips mapped
  onto it, marked `short`), front first. `gap_s` on a bus is the time the next running
  bus behind it needs to reach where it is now (buses waiting at a terminus or stale
  don't count). `pending` lists departures due from that direction's first stop.
- `GET /api/stops?q=…&near=lat,lon&limit=20`: stops on tracked routes, matched by any
  words of the name or code (accents and case ignored) and/or sorted by distance
- `GET /api/stops/{code}`: every tracked bus still heading to the stop, with our ETA
  (`eta_s`, with `eta_src` `history` or `speed`), the operator's (`operator`, fetched
  live and cached 20 s) and later
  timetable passes. Untracked runs the operator lists appear with `live: false`.
- `GET /api/trips/{trip}`: one bus, the stops ahead with ETAs, and its recent fixes
- `GET /api/status`: last GPS batch, batch interval, stale/stopped counts, stops with
  no code (a hint that a resync is due), and removed buses: `drops_by_reason` since
  start and `drops_recent` (trip, reason, how long it stood still, where). Reasons:
  `next_run` (its vehicle started a newer trip), `layover_timeout`, `terminus_timeout`, `midroute_timeout`, `not_reported` (no stop lists it for 3 min), `blind_timeout` (see Stop codes),
  `vehicle_reused` (its vehicle runs a newer trip). Each removal is also logged. Also
  `stop_list` (last download, ok), `route_checks_last_hour`, `history` (stretches
  learned, usable, runs) and `eta_errors` (see Learned stretch times).
- `GET /api/debug`: poll set, batch intervals, raw fix history

The page uses hash links that can be shared: `/#/stop/09001`, `/#/bus/T06:1086`.

## Keeping data fresh (resync)

The server keeps itself current (see Stop codes and Route checks above): the stop list
weekly, route shapes daily per variant and on off-route GPS. A manual resync is only
needed if that can't work, e.g. the stop list moved or the server was down for weeks:

```
python3 tools/scan_stops.py    # full rescan (~35 min)
python3 tools/fill_stops.py    # probe gaps until every route stop has a code
rm data/routes.json            # refetch route shapes on next start
python3 tools/check_coverage.py  # poll every route stop: are buses missing?
```

After a stop-list download the log says how many stops were new, moved or no longer
listed; route checks log `route check <trip> (<why>): unchanged` or the new variant.

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
- Installing it as an app (Chrome on Android: ⋮ → "Install app"; Safari on iPhone: Share →
  "Add to Home Screen") needs HTTPS, so put it behind e.g. `tailscale serve 8080` or a
  Cloudflare Tunnel; over plain `http://<host>:8080` Chrome won't offer it. The page's
  service worker (`web/sw.js`) is network first: pages always come fresh from the server,
  `/api` is never cached, and the cached shell only opens the page offline.

## Next steps
- Check `eta_errors` in `/api/status` after a few days of learned stretch times; tune
  the bucket sizes and the pace factor if ours isn't clearly ahead.
- Android thin client: widget, Wear OS tile, "leave now" notification.
