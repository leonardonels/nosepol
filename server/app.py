#!/usr/bin/env python3
"""NoSePol backend: TPL FVG live-bus POC.

- Discovers the target lines' route variants via /polemonitor/LineGeoTrack.
- Watches a heartbeat stop to detect each global GPS batch (~every 20-25 s),
  then sweeps a set of stops along each variant once per batch.
- Projects each bus's GPS onto its route and fits s(t) by least squares.
- Serves JSON at /api/... and the map page from ../web.

Stdlib only:  python3 server/app.py  [--port 8080] [--lines T06,T08,T09]
"""
import argparse
import shutil
import hashlib
import json
import logging
import math
import pathlib
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from estimator import Track
from geo import Frame, Polyline

API = "https://realtime.tplfvg.it/API/v1.0/polemonitor"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEED = ROOT / "seed"     # bundled copy of data/ (Docker image only)
WEB = ROOT / "web"
FRAME = Frame(45.65, 13.78)  # Trieste
HEARTBEAT_EVERY = 5     # s
FORCE_SWEEP_EVERY = 60  # s, sweep even if heartbeat sees no change
STOP_STRIDE = 4         # poll every Nth stop of each variant (+ terminus)
TRIP_TTL = 180          # s not listed, or standing still at a terminus -> drop trip
STOPPED_TTL = 600       # s standing still mid-route -> drop trip
TERMINUS_M = 150        # within this of either end of the route counts as "at the terminus"
STOP_MATCH_M = 40

log = logging.getLogger("nosepol")


def get(path, **params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "NoSePol/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


class Variant:
    """One route shape of a line (direction / short-turn), with its stop sequence."""

    def __init__(self, line, direction, destination, segments, stopdb):
        self.line, self.direction, self.destination = line, direction, destination
        self.segments = segments
        coords = [(lat, lon) for seg in segments for lon, lat in seg]
        self.shape = Polyline(coords, FRAME)
        # segment boundaries are stops: start of each segment + end of the last
        ends = [seg[0] for seg in segments] + [segments[-1][-1]]
        self.stops = []
        for lon, lat in ends:
            _, s = self.shape.project(lat, lon, self.stops[-1]["s"] if self.stops else 0)
            code, name = stopdb.nearest(lat, lon)
            name = name or f"stop {len(self.stops) + 1} (code unknown)"
            self.stops.append({"code": code, "name": name, "lat": lat, "lon": lon, "s": round(s, 1)})
        sig = "|".join(f"{lat:.4f},{lon:.4f}" for lon, lat in ends)
        self.key = f"{line}-{direction}-{hashlib.sha1(sig.encode()).hexdigest()[:6]}"

    def next_stop(self, s):
        return next((st for st in self.stops if st["s"] > s + 5), None)

    def poll_stops(self):
        codes = [st["code"] for st in self.stops if st["code"]]
        picked = codes[STOP_STRIDE::STOP_STRIDE] + codes[-1:]
        return list(dict.fromkeys(picked))

    def summary(self):
        return {"key": self.key, "line": self.line, "direction": self.direction,
                "destination": self.destination, "length": round(self.shape.length),
                "from": self.stops[0]["name"], "to": self.stops[-1]["name"],
                "n_stops": len(self.stops)}

    def geojson(self):
        return {**self.summary(),
                "coords": [[round(la, 6), round(lo, 6)] for seg in self.segments for lo, la in seg],
                "stops": self.stops}


class StopDB:
    def __init__(self, path):
        self.stops = list(json.loads(path.read_text()).values()) if path.exists() else []
        for st in self.stops:
            st["xy"] = FRAME.xy(st["lat"], st["lon"])
        log.info("stop db: %d stops", len(self.stops))

    def nearest(self, lat, lon):
        p = FRAME.xy(lat, lon)
        best = min(self.stops, key=lambda st: math.dist(p, st["xy"]), default=None)
        if best and math.dist(p, best["xy"]) <= STOP_MATCH_M:
            return best["code"], best["name"]
        return None, None

    def by_distance(self, lat, lon):
        p = FRAME.xy(lat, lon)
        return sorted((st for st in self.stops if st.get("urban")), key=lambda st: math.dist(p, st["xy"]))


class Tracker:
    def __init__(self, lines):
        self.lines = lines
        self.lock = threading.RLock()
        self.stopdb = StopDB(DATA / "stops.json")
        self.variants = {}      # key -> Variant
        self.race_variant = {}  # "T06:123" -> key
        self.tracks = {}        # trip -> Track
        self.seen = {}          # trip -> last time seen at any stop
        self.frozen = {}        # dropped trip -> its last (lat, lon), ignored until it moves
        self.stats = {"sweeps": 0, "requests": 0, "last_sweep": None, "last_batch": None,
                      "batch_intervals": []}
        self.pool = ThreadPoolExecutor(4)
        self._load_cache()

    def wants(self, line):
        """T06 also covers its short-turn variants such as 'T06/'."""
        return line.rstrip("/") in self.lines

    # ---- route cache -------------------------------------------------
    def _load_cache(self):
        p = DATA / "routes.json"
        if not p.exists():
            return
        d = json.loads(p.read_text())
        for v in d["variants"]:
            if self.wants(v["line"]):
                var = Variant(v["line"], v["direction"], v["destination"], v["segments"], self.stopdb)
                self.variants[var.key] = var
        self.race_variant = {k: v for k, v in d["races"].items() if v in self.variants}
        log.info("route cache: %d variants, %d races", len(self.variants), len(self.race_variant))

    def _save_cache(self):
        DATA.mkdir(exist_ok=True)
        (DATA / "routes.json").write_text(json.dumps({
            "variants": [{"line": v.line, "direction": v.direction, "destination": v.destination,
                          "segments": v.segments} for v in self.variants.values()],
            "races": self.race_variant}))

    def variant_for(self, run):
        trip = f"{run['Line']}:{run['Race']}"
        key = self.race_variant.get(trip)
        if key:
            return self.variants[key]
        segs = get("LineGeoTrack", Line=run["Line"], Race=run["Race"])
        self.stats["requests"] += 1
        if not segs:
            return None
        var = Variant(run["Line"], run["Direction"], run["Destination"], segs, self.stopdb)
        with self.lock:
            if var.key not in self.variants:
                log.info("new variant %s: %s -> %s (%d stops, %.1f km)", var.key,
                         var.stops[0]["name"], var.stops[-1]["name"], len(var.stops),
                         var.shape.length / 1000)
                self.variants[var.key] = var
            self.race_variant[trip] = var.key
            self._save_cache()
        return self.variants[var.key]

    def poll_set(self):
        with self.lock:
            codes = [c for v in self.variants.values() for c in v.poll_stops()]
        return list(dict.fromkeys(codes))

    # ---- discovery ---------------------------------------------------
    def discover(self):
        """Walk stops outward from the centre until every target line has a
        variant in both directions (or we give up)."""
        def missing():
            have = {(v.line.rstrip("/"), v.direction) for v in self.variants.values()}
            return [(l, d) for l in self.lines for d in "AR" if (l, d) not in have]

        if not missing():
            return
        log.info("discovering routes for %s ...", missing())
        for i, st in enumerate(self.stopdb.by_distance(45.6505, 13.7765)):
            if not missing() or i > 1500:
                break
            try:
                runs = get("mrcruns", StopCode=st["code"], IsUrban="true") or []
            except Exception as e:
                log.warning("discover %s: %s", st["code"], e)
                continue
            for run in runs:
                if (run["Line"].rstrip("/"), run["Direction"]) in missing():
                    self.variant_for(run)
            time.sleep(0.1)
        if missing():
            log.warning("could not find variants for %s", missing())

    # ---- polling -----------------------------------------------------
    def _fetch(self, code):
        try:
            self.stats["requests"] += 1
            return code, get("mrcruns", StopCode=code, IsUrban="true") or []
        except Exception as e:
            log.warning("stop %s: %s", code, e)
            return code, []

    def sweep(self, t):
        codes = self.poll_set()
        listed = {}
        for code, rs in self.pool.map(self._fetch, codes):
            for r in rs:
                if self.wants(r["Line"]):
                    listed.setdefault(f"{r['Line']}:{r['Race']}", []).append(r)
        runs = {trip: self._freshest(trip, rs) for trip, rs in listed.items()}
        updated = 0
        for trip, r in runs.items():
            if not r.get("IsStarted") or not r.get("Latitude"):
                continue
            if trip in self.frozen:
                if self.frozen[trip] == (r["Latitude"], r["Longitude"]):
                    continue  # dropped ghost, position still frozen
                del self.frozen[trip]  # it moved again: track it afresh
            try:
                var = self.variant_for(r)
            except Exception as e:
                log.warning("track %s: %s", trip, e)
                continue
            if not var:
                continue
            with self.lock:
                tr = self.tracks.get(trip)
                if tr is None or tr.variant is not var:
                    tr = self.tracks[trip] = Track(trip, var)
                tr.meta = {"line": r["Line"], "vehicle": r["Vehicle"], "direction": r["Direction"],
                           "destination": r["Destination"], "note": r["Note"] or None,
                           "api_arrival": r["ArrivalTime"]}
                updated += tr.add_fix(t, r["Latitude"], r["Longitude"])
                self.seen[trip] = t
        with self.lock:
            stale = {k for k, ts in self.seen.items() if t - ts > TRIP_TTL}
            stale |= {k for k, tr in self.tracks.items() if self._drop_still(tr, t)}
            for trip in stale:
                self.seen.pop(trip, None)
                tr = self.tracks.pop(trip, None)
                if tr:
                    self.frozen[trip] = tr.fixes[-1][3:5]
            self.stats["sweeps"] += 1
            self.stats["last_sweep"] = {"t": t, "stops": len(codes), "trips": len(runs),
                                        "new_fixes": updated}
        log.info("sweep: %d stops, %d trips, %d new fixes", len(codes), len(runs), updated)

    def _freshest(self, trip, rs):
        """A stop the bus already passed can keep listing it with a frozen position,
        so when stops disagree, take a position that differs from the last fix."""
        tr = self.tracks.get(trip)
        if tr and len(rs) > 1:
            last = tr.fixes[-1][3:5]
            moved = [r for r in rs if (r.get("Latitude"), r.get("Longitude")) != last]
            if moved:
                return moved[0]
        return rs[0]

    def _drop_still(self, tr, t):
        """A bus that stopped moving: drop it at a terminus (layover, or a finished trip
        still listed) or when its vehicle runs a newer trip; mid-route, keep it as
        "stopped" for up to STOPPED_TTL."""
        still = t - tr.fixes[-1][0]
        if still > STOPPED_TTL:
            return True
        if still <= TRIP_TTL:
            return False
        s = tr.fixes[-1][1]
        if s < TERMINUS_M or s > tr.variant.shape.length - TERMINUS_M:
            return True
        veh = tr.meta.get("vehicle")
        return bool(veh) and any(o is not tr and o.meta.get("vehicle") == veh
                                 and o.fixes[-1][0] > tr.fixes[-1][0]
                                 for o in self.tracks.values())

    def run(self):
        self.discover()
        last_sig, last_sweep = None, 0
        while True:
            t = time.time()
            codes = self.poll_set()
            sig = None
            if codes:
                _, rs = self._fetch(codes[len(codes) // 2])
                sig = tuple(sorted((r["Race"], r["Latitude"], r["Longitude"]) for r in rs if r.get("IsStarted")))
            changed = sig and last_sig is not None and sig != last_sig
            if changed:
                lb = self.stats["last_batch"]
                if lb:
                    self.stats["batch_intervals"] = (self.stats["batch_intervals"] + [round(t - lb)])[-20:]
                self.stats["last_batch"] = t
            if changed or t - last_sweep > FORCE_SWEEP_EVERY or last_sig is None:
                try:
                    self.sweep(t)
                except Exception:
                    log.exception("sweep failed")
                last_sweep = t
            last_sig = sig
            time.sleep(max(0.0, HEARTBEAT_EVERY - (time.time() - t)))

    # ---- views -------------------------------------------------------
    def vehicles(self, line=None):
        now = time.time()
        with self.lock:
            out = [tr.state(now) for tr in self.tracks.values() if line in (None, tr.variant.line.rstrip("/"))]
        return {"t": now, "vehicles": sorted(out, key=lambda v: (v["line"], v["variant"], v["s"]))}

    def lines_summary(self):
        with self.lock:
            return [{"line": l,
                     "variants": [v.summary() for v in self.variants.values() if v.line.rstrip("/") == l],
                     "vehicles": sum(tr.variant.line.rstrip("/") == l for tr in self.tracks.values())}
                    for l in self.lines]

    def shapes(self, line):
        with self.lock:
            return [v.geojson() for v in self.variants.values() if line in (None, v.line.rstrip("/"))]


def make_handler(tracker):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(WEB), **kw)

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
            if not parts or parts[0] != "api":
                return super().do_GET()
            match parts[1:]:
                case ["lines"]:
                    return self._json(tracker.lines_summary())
                case ["vehicles"]:
                    return self._json(tracker.vehicles())
                case ["shapes"]:
                    return self._json(tracker.shapes(None))
                case ["lines", line, "vehicles"]:
                    return self._json(tracker.vehicles(line))
                case ["lines", line, "shapes"]:
                    return self._json(tracker.shapes(line))
                case ["debug"]:
                    with tracker.lock:
                        return self._json({**tracker.stats, "poll_set": tracker.poll_set(),
                                           "tracks": {k: [f[:3] for f in tr.fixes]
                                                      for k, tr in tracker.tracks.items()}})
            self._json({"error": "not found"}, 404)

    return H


def seed_data():
    """Fill missing data files from the bundled seed, e.g. when an empty host folder is
    bind-mounted over data/. Existing files are never overwritten."""
    if not SEED.is_dir():
        return
    DATA.mkdir(exist_ok=True)
    for src in sorted(SEED.glob("*.json")):
        dst = DATA / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            log.info("seeded %s from the image", src.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lines", default="T06,T08,T09")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    seed_data()
    tracker = Tracker(args.lines.split(","))
    threading.Thread(target=tracker.run, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(tracker))
    log.info("http://localhost:%d", args.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
