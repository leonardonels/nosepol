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
import collections
import shutil
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import statistics
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from estimator import ARRIVE_M, DWELL_S, ESTIMATOR, OFFROUTE_M, Blend, History, Track
from kalman import DelayFilter, TimetableTrack
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
POLL_GAP_M = 1000       # ...and more where that leaves more than this between polled stops
TRIP_TTL = 180          # s not listed anywhere (or its vehicle runs a newer trip) -> drop trip
TERMINUS_TTL = 15 * 60  # s standing still at a terminus (normal layover) -> drop trip
STOPPED_TTL = 60 * 60   # s standing still mid-route (may have a problem) -> drop trip
TERMINUS_M = 150        # near either end of the route (also: at/past the second-to-last stop) = terminus
STOP_MATCH_M = 40
OPERATOR_CACHE_S = 20   # s to reuse a stop's live operator answer
AT_STOP_M = 20          # a bus this close past a stop still counts as arriving there
STALE_S = 90            # GPS older than this (and not "stopped") is stale
LAYOVER_TTL = 15 * 60  # s a bus waiting at the terminus after its trip stays, unless its next run starts
LATE_OK = 5 * 60        # s an incoming bus may reach the terminus after a departure and still run it
PENDING_AHEAD = 20 * 60  # s ahead to show a departure no known bus will take
PENDING_LATE = 10 * 60   # s after its time to keep showing it (late, or gone unseen)
BLIND_SPEED = 3.0       # m/s, slowest speed assumed to cover a stretch no stop reports on
BLIND_MAX_S = 45 * 60   # s, longest a bus is kept on such a stretch without a report
FILL_NEAR_M = 2000      # look for missing stop codes in districts with a stop this close
FILL_GAP = 60           # codes probed past a district's last known code
FILL_EVERY = 24 * 3600  # s between code searches, only while the stop list can't be downloaded
STOPS_URL = "https://tplfvg.it/services/geojson/points/?lang=it"  # the operator's public stop list
STOPS_EVERY = 7 * 24 * 3600  # s between downloads of it (one ~4 MB request, not the realtime API)
STOPS_SOON = 6 * 3600   # s, earliest download again while some route stop has no code
STOPS_RETRY = 3600      # s after a failed download
STOPS_NEAR_KM = 25      # urban stops this close to Trieste are kept (line 44 ends at ~21 km)
ROUTE_CHECK_EVERY = 24 * 3600  # s: per route variant, one trip's shape is fetched again this often
OFFROUTE_FIXES = 3      # this many fixes in a row off the shape: fetch the trip's shape again
RECHECK_PER_HOUR = 20   # cap on those extra shape requests
ROUTE_UNUSED_S = 21 * 24 * 3600  # a variant no trip ran on for this long is dropped
DATA_EVERY = 600        # s between data housekeeping rounds (saves, refresh checks)
STREAM_EVERY = 3        # s between updates on /api/stream (the page glides markers over this)
STREAM_MAX_S = 10 * 60  # s before a stream closes; the browser reconnects on its own
MAX_STREAMS = 200       # open streams at once; more get 503 and the page polls instead
LOCAL_TZ = ZoneInfo("Europe/Rome")  # the operator's timetable times are local
# Timetable filter (kalman.py): every stop's listing gives the run's scheduled time there.
FROZEN_AFTER_S = 150    # s listed at the same position mid-route: GPS frozen, move it on the timetable
KF_TTL = 6 * 60         # s a bus with a known timetable is kept while no stop lists it (hidden, GPS gap)
GHOST_DUE_S = 120       # a run with no GPS due this soon (or past) at a stop it's listed at is out running
GHOST_TTL = 4 * 60      # s a ghost is kept after the last stop listing it
PASSAGE_GAP_S = 120     # longest gap between two polls of a stop that times a passage
PASSAGE_NEAR_M = 1500   # a passage counts only this close to where the filter has the bus
# Every FULL_EVERY-th sweep polls the full set; the others only the stops just ahead of
# known buses and the termini (1: always full, as before).
FULL_EVERY = max(1, int(os.environ.get("NOSEPOL_FULL_EVERY", "3")))
# Line colours by position in --lines: (fill, text on it). Order is the operator's choice.
PALETTE = [("#F07B3F", "#1B1B1F"), ("#4A73D6", "#FFFFFF"), ("#8BC34A", "#1B1B1F"),
           ("#D35DA2", "#FFFFFF"), ("#26A6B5", "#1B1B1F"), ("#F2B33D", "#1B1B1F"),
           ("#8E6CE0", "#FFFFFF"), ("#3E9E5E", "#FFFFFF")]

log = logging.getLogger("nosepol")


def fold(text):
    """Lower case, no accents, punctuation as spaces: for matching search words."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def pretty(name):
    """'TRIESTE VIA BATTISTI (GALLERIA FENICE)' -> 'Via Battisti (Galleria Fenice)'."""
    name = re.sub(r"^TRIESTE\s+", "", (name or "").strip())
    if name.isupper():
        name = name.title()
    return name[:1].upper() + name[1:]


def line_name(line):
    """'T06' -> '6', 'T06/' -> '6' (a short turn is shown like its line)."""
    return re.sub(r"^T0*", "", line.rstrip("/")) or line


def operator_eta(arrival):
    """Seconds from the operator's ArrivalTime text: '3 min' -> 180, 'In transito' -> 0."""
    if not arrival:
        return None
    m = re.match(r"\s*(\d+)\s*min", arrival)
    if m:
        return int(m.group(1)) * 60
    return 0 if "transito" in arrival.lower() else None


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
        # the whole path: a detour can keep the same stops (same key) on another road
        path = "|".join(f"{lat:.5f},{lon:.5f}" for seg in segments for lon, lat in seg)
        self.sig = hashlib.sha1(path.encode()).hexdigest()[:12]

    def next_stop(self, s):
        return next((st for st in self.stops if st["s"] > s + 5), None)

    def poll_stops(self):
        """Every STOP_STRIDE-th coded stop and the last two, plus a stop wherever
        skipping it would leave more than POLL_GAP_M of route unpolled. A stop lists
        only the next bus per line and direction, so between two polled stops only
        the bus nearest the second one is seen (stop_behind finds the others). The
        terminus lists its next departure rather than buses arriving (seen on line 8
        at Valmaura), so the stop before it covers the last stretch."""
        coded = [st for st in self.stops if st["code"]]
        picked, last = [], coded[0]["s"] if coded else 0.0
        for i, st in enumerate(coded):
            nxt = coded[i + 1]["s"] if i + 1 < len(coded) else None
            if (i and i % STOP_STRIDE == 0) or i >= len(coded) - 2 or nxt - last > POLL_GAP_M:
                picked.append(st["code"])
                last = st["s"]
        return list(dict.fromkeys(picked))

    def stop_behind(self, s):
        """The nearest coded stop a bus at s has passed. It lists the next bus coming
        after this one, which no other polled stop may show (it is hidden behind this one)."""
        return next((st["code"] for st in reversed(self.stops) if st["code"] and st["s"] < s - AT_STOP_M), None)

    @property
    def blind_from(self):
        """Where an end stretch with no coded stop starts (no stop can report a bus
        there), or None when the last stop has a code."""
        if self.stops[-1]["code"]:
            return None
        return max((st["s"] for st in self.stops if st["code"]), default=0.0)

    def refresh_codes(self, stopdb):
        """Match stops that had no code again, after the stop db learned new ones."""
        n = 0
        for st in self.stops:
            if not st["code"]:
                code, name = stopdb.nearest(st["lat"], st["lon"])
                if code:
                    st["code"], st["name"] = code, name
                    n += 1
        return n

    def summary(self):
        return {"key": self.key, "line": self.line, "direction": self.direction,
                "destination": self.destination, "headsign": pretty(self.destination),
                "length": round(self.shape.length),
                "from": self.stops[0]["name"], "to": self.stops[-1]["name"],
                "n_stops": len(self.stops)}

    def geojson(self):
        return {**self.summary(),
                "coords": [[round(la, 6), round(lo, 6)] for seg in self.segments for lo, la in seg],
                "stops": self.stops}


class StopDB:
    def __init__(self, path):
        self.path = path
        self.stops = list(json.loads(path.read_text()).values()) if path.exists() else []
        for st in self.stops:
            st["xy"] = FRAME.xy(st["lat"], st["lon"])
        log.info("stop db: %d stops", len(self.stops))

    def add(self, code, name, lat, lon, urban):
        self.stops.append({"code": code, "name": name, "lat": lat, "lon": lon, "urban": urban,
                           "xy": FRAME.xy(lat, lon)})

    def merge(self, features):
        """Add the stops of the operator's list (GeoJSON features) that aren't known yet,
        and move known ones that moved. Stops missing from the list are only counted:
        a stop that's gone does no harm, one dropped by mistake would. Returns
        (added, moved, missing)."""
        known = {st["code"]: st for st in self.stops}
        added = moved = 0
        listed = set()
        for f in features:
            pr, (lon, lat) = f.get("properties") or {}, f["geometry"]["coordinates"]
            code = pr.get("code")
            if not code or "urban" not in (pr.get("services") or []):
                continue
            if math.dist(FRAME.xy(lat, lon), (0, 0)) > STOPS_NEAR_KM * 1000:
                continue
            listed.add(code)
            st = known.get(code)
            if st is None:
                self.add(code, pr.get("name"), lat, lon, True)
                added += 1
            elif math.dist(FRAME.xy(lat, lon), st["xy"]) > 30:
                st["lat"], st["lon"], st["xy"] = lat, lon, FRAME.xy(lat, lon)
                moved += 1
        missing = sum(1 for st in self.stops if st.get("urban") and st["code"] not in listed
                      and math.dist(st["xy"], (0, 0)) <= STOPS_NEAR_KM * 1000)
        return added, moved, missing

    def save(self):
        out = {st["code"]: {k: v for k, v in st.items() if k != "xy"} for st in self.stops}
        self.path.write_text(json.dumps(out, indent=0, ensure_ascii=False))

    def nearest(self, lat, lon):
        p = FRAME.xy(lat, lon)
        best = min(self.stops, key=lambda st: math.dist(p, st["xy"]), default=None)
        if best and math.dist(p, best["xy"]) <= STOP_MATCH_M:
            return best["code"], best["name"]
        return None, None

    def by_distance(self, lat, lon):
        p = FRAME.xy(lat, lon)
        return sorted((st for st in self.stops if st.get("urban")), key=lambda st: math.dist(p, st["xy"]))


def stop_index(var, code, s_hint=0.0):
    """Index of the stop with code on var: the first at or past s_hint (loops pass twice)."""
    idx = [i for i, st in enumerate(var.stops) if st["code"] == code]
    if not idx:
        return None
    return next((i for i in idx if var.stops[i]["s"] >= s_hint), idx[-1])


class Ghost:
    """A run the feed lists without any position, though the timetable says it is out on
    the road: its bus runs with no GPS. Placed by its timetable and delay filter alone,
    corrected by passages (a stop stops listing it once it has gone by)."""

    def __init__(self, trip, variant, t):
        self.trip, self.variant = trip, variant
        self.tl = TimetableTrack(variant)
        self.tl.kf = DelayFilter(t)
        self.first_seen = self.last_listed = t
        self.meta = {}
        self.layover = False

    def s(self, now):
        return self.tl.position(now)[0] if self.tl.schedule() else 0.0

    def state(self, now):
        s, sm, pace = self.tl.position(now)
        lat, lon = self.variant.shape.at(s)
        nxt = self.variant.next_stop(s)
        eta = self.tl.eta(nxt["s"], now) if nxt else None
        d, sd = self.tl.delay(now)
        return {"trip": self.trip, "variant": self.variant.key, "s": round(s, 1),
                "length": round(self.variant.shape.length), "lat": round(lat, 6), "lon": round(lon, 6),
                "v": round(pace, 2), "heading": None if (h := self.variant.shape.heading(s)) is None else round(h),
                "residual": None, "n_fixes": 0, "age": None, "stopped_s": None, "next_run": None,
                "blind": False, "estimated": True, "gps_frozen": False, "gps_lost": True,
                "ghost": True, "confirmed": self.tl.passages > 0,
                "timetable": {"delay_s": round(d), "sigma_s": round(sd), "sigma_m": round(sd * pace),
                              "points": len(self.tl.tt.at), "passages": self.tl.passages},
                "gps": None,
                "next_stop": nxt and {"code": nxt["code"], "name": nxt["name"], "dist": round(nxt["s"] - s),
                                      "eta_s": None if eta is None else round(eta)},
                **self.meta}


class Tracker:
    def __init__(self, lines):
        self.lines = lines
        self.lock = threading.RLock()
        self.stopdb = StopDB(DATA / "stops.json")
        self.variants = {}      # key -> Variant
        self.race_variant = {}  # "T06:123" -> key
        self.tracks = {}        # trip -> Track
        self.ghosts = {}        # trip -> Ghost: listed with no GPS but running by the timetable
        self._listing = {}      # stop code -> (time polled, {trip: (line, direction)} it listed)
        self._past_tt = {}      # variant key -> recent finished trips' Timetables (to extend others')
        self.kf_stats = {"passages": 0, "passage_z": [], "ghosts_found": 0, "ghosts_then_gps": 0,
                         "frozen": 0, "full_sweeps": 0, "targeted_sweeps": 0}
        self.seen = {}          # trip -> last time seen at any stop
        self.frozen = {}        # dropped trip -> its last (lat, lon), ignored until it moves
        self.drops = collections.deque(maxlen=50)  # recent removals, with the reason
        self.drop_counts = {}   # reason -> removals since start
        self.started = time.time()
        self.pending = []      # departures due soon that no known bus will take
        self.stats = {"sweeps": 0, "requests": 0, "last_sweep": None, "last_batch": None,
                      "batch_intervals": []}
        self.pool = ThreadPoolExecutor(4)
        self._stop_index = (None, {})  # (variant keys it was built from, code -> entry)
        self._operator = {}            # stop code -> (time fetched, runs)
        self._live, self._live_lock = None, threading.Lock()  # (built at, body) for live()
        self.checked = {}       # variant key -> last time one of its trips' shape was fetched
        self.used = {}          # variant key -> last time a bus was tracked on it
        self.routes_version = 0  # goes up whenever a route shape is added, changed or dropped
        self._rechecked = {}    # trip -> last time its shape was fetched again
        self._recheck_times = collections.deque()  # those fetches in the last hour
        self.history = History(DATA / "speeds.json")
        self.blend = Track.blend = Blend(DATA / "blend.json")
        self._preds = {}        # (trip, stop index) -> [(made at, speed-only arrival, our arrival)]
        self._pred_at = {}      # trip -> when its predictions were last made
        # recent scored arrivals: (horizon, line, GPS age band, then the absolute errors of
        # speed, history, timetable, blend in s), for the median and 95th percentile beside
        # the mean (the bad cases matter most), by horizon, line and GPS age
        self.eta_samples = collections.deque(maxlen=self.ETA_SAMPLES)
        self.eta_errors = {}    # horizon -> {"n", "speed", "ours", "nk", "kalman", "ours_k"}: absolute errors summed (s)
        # when the stop list was downloaded, and per variant when it was checked and last used
        self.refresh = self._load_json(DATA / "refresh.json")
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
        now = time.time()
        self.checked = {k: t for k, t in self.refresh.get("checked", {}).items() if k in self.variants}
        # a variant with no record counts as used now, so it isn't dropped at once
        self.used = {k: self.refresh.get("used", {}).get(k, now) for k in self.variants}
        self.routes_version = len(self.variants)
        log.info("route cache: %d variants, %d races", len(self.variants), len(self.race_variant))

    @staticmethod
    def _load_json(path):
        try:
            return json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError) as e:
            log.warning("reading %s: %s", path.name, e)
            return {}

    def _save_cache(self):
        DATA.mkdir(exist_ok=True)
        (DATA / "routes.json").write_text(json.dumps({
            "variants": [{"line": v.line, "direction": v.direction, "destination": v.destination,
                          "segments": v.segments} for v in self.variants.values()],
            "races": self.race_variant}))

    def _save_refresh(self):
        self.refresh["checked"] = {k: round(t) for k, t in self.checked.items()}
        self.refresh["used"] = {k: round(t) for k, t in self.used.items()}
        (DATA / "refresh.json").write_text(json.dumps(self.refresh))

    def variant_for(self, run):
        trip = f"{run['Line']}:{run['Race']}"
        key = self.race_variant.get(trip)
        if key:
            return self.variants[key]
        segs = get("LineGeoTrack", Line=run["Line"], Race=run["Race"])
        self.stats["requests"] += 1
        if not segs:
            return None
        return self._adopt(trip, run["Direction"], run["Destination"], segs)

    def _adopt(self, trip, direction, destination, segs, recheck=False):
        """Map trip to the variant of the shape segs, adding or replacing it. On a
        recheck (the trip's shape fetched again), a trip now on other stops than its
        variant's means that variant changed: its other trips are fetched again too,
        each when it next runs, so the requests stay spread out."""
        var = Variant(trip.rpartition(":")[0], direction, destination, segs, self.stopdb)
        with self.lock:
            old = self.race_variant.get(trip)
            cur = self.variants.get(var.key)
            if cur is None or cur.sig != var.sig:
                log.info("%s variant %s: %s -> %s (%d stops, %.1f km)", "new" if cur is None else "changed path of",
                         var.key, var.stops[0]["name"], var.stops[-1]["name"], len(var.stops),
                         var.shape.length / 1000)
                self.variants[var.key] = var
                self._stop_index = (None, {})
                self.routes_version += 1
            else:
                var = cur
            if recheck and old and old != var.key:
                stale = [k for k, v in self.race_variant.items() if v == old and k != trip]
                for k in stale:
                    del self.race_variant[k]
                log.info("trip %s moved from %s to %s: %d more trips of it will be fetched again",
                         trip, old, var.key, len(stale))
            self.race_variant[trip] = var.key
            now = time.time()
            self.checked[var.key] = now
            self.used.setdefault(var.key, now)
            self._save_cache()
        return var

    def recheck(self, tr, why, t):
        """Fetch a tracked trip's shape again (at most once a day per trip, and
        RECHECK_PER_HOUR in all), in case its route changed."""
        trip = tr.trip
        if t - self._rechecked.get(trip, 0) < ROUTE_CHECK_EVERY:
            return
        while self._recheck_times and t - self._recheck_times[0] > 3600:
            self._recheck_times.popleft()
        if len(self._recheck_times) >= RECHECK_PER_HOUR:
            return
        self._rechecked[trip] = t
        self._recheck_times.append(t)
        line, _, race = trip.rpartition(":")
        try:
            self.stats["requests"] += 1
            segs = get("LineGeoTrack", Line=line, Race=race)
        except Exception as e:
            log.warning("route check %s: %s", trip, e)
            return
        if not segs:
            return
        before = tr.variant
        var = self._adopt(trip, tr.meta.get("direction") or before.direction,
                          tr.meta.get("destination") or before.destination, segs, recheck=True)
        log.info("route check %s (%s): %s", trip, why, "unchanged" if var is before else f"now {var.key}")

    def _route_checks(self, t, moved):
        """After a sweep: fetch again the shape of a bus that left its route, and of
        one trip per variant not checked for ROUTE_CHECK_EVERY (one per sweep)."""
        with self.lock:
            off = [tr for tr in moved if len(tr.fixes) >= OFFROUTE_FIXES
                   and all(f[2] > OFFROUTE_M for f in tr.fixes[-OFFROUTE_FIXES:])]
            due = [tr for tr in moved if t - self.checked.get(tr.variant.key, 0) > ROUTE_CHECK_EVERY
                   and tr not in off]
            for tr in moved:
                self.used[tr.variant.key] = t
        for tr in off:
            self.recheck(tr, "off route", t)
        if due:
            self.recheck(due[0], "daily check", t)
            self.checked.setdefault(due[0].variant.key, 0)
            if t - self.checked[due[0].variant.key] > ROUTE_CHECK_EVERY:
                # capped or failed: try another trip of it later, not every sweep
                self.checked[due[0].variant.key] = t - ROUTE_CHECK_EVERY + 3600

    ETA_HORIZONS = ((180, "0-3 min"), (600, "3-10 min"), (1800, "10-30 min"))
    ETA_EVERY = 60    # s between predictions of one bus
    ETA_AHEAD = 8     # stops ahead predicted
    ETA_SAMPLES = 20000  # recent scored arrivals kept for percentiles
    # GPS age when the prediction was made: fresh, one or two batches missed, old (the
    # position is estimated from 90 s), long gone
    GPS_AGE_BANDS = ((30, "<30 s"), (90, "30-90 s"), (180, "90-180 s"), (float("inf"), "180 s+"))

    def _score_etas(self, t):
        """Measure the ETAs against real arrivals: every ETA_EVERY, note each moving
        bus's predicted arrival at its next stops, both speed-only (time_to, the old
        way) and ours (eta); when it arrives, add the errors by how far ahead the
        prediction was made. Shown in /api/status as eta_errors."""
        for trip, tr in self.tracks.items():
            for i, ta in tr.new_arrivals:
                for made, old, new, kf, bl, line, ageb in self._preds.pop((trip, i), []):
                    ahead = ta - made
                    h = next((name for lim, name in self.ETA_HORIZONS if ahead <= lim), None)
                    if h is None or ahead < 15:
                        continue
                    e = self.eta_errors.setdefault(h, {"n": 0, "speed": 0.0, "ours": 0.0,
                                                       "nk": 0, "kalman": 0.0, "ours_k": 0.0, "blend": 0.0})
                    e["n"] += 1
                    e["speed"] += abs(old - ta)
                    e["ours"] += abs(new - ta)
                    if kf is not None:
                        e["nk"] += 1
                        e["kalman"] += abs(kf - ta)
                        e["ours_k"] += abs(new - ta)
                        e["blend"] = e.get("blend", 0.0) + abs(bl - ta)
                        self.eta_samples.append((h, line, ageb, abs(old - ta), abs(new - ta),
                                                 abs(kf - ta), abs(bl - ta)))
                        self.blend.add(ahead, new - ta, kf - ta, made, line)  # weights learn after scoring this one
            tr.new_arrivals.clear()
            if tr.layover or tr.stopped_for(t) is not None or t - self._pred_at.get(trip, 0) < self.ETA_EVERY:
                continue
            s, v, *_ = tr.predict(t)
            if not v or v <= 0.5:
                continue
            self._pred_at[trip] = t
            line = tr.variant.line.rstrip("/")
            age = t - tr.fixes[-1][0]
            ageb = next(name for lim, name in self.GPS_AGE_BANDS if age < lim)
            est = tr.estimated(t)
            n = 0
            for i, st in enumerate(tr.variant.stops):
                if st["s"] - ARRIVE_M <= s or i in tr.arrivals:
                    continue
                old, new = tr.time_to(s, st["s"], v), tr.eta_hist(s, st["s"], v, t)[0]
                kf = tr.tl.eta(st["s"], t) if tr.tl.ready() else None
                if old is not None and new is not None:
                    # "blend" is what the app serves: the timetable's alone when GPS is old
                    bl = None if kf is None else t + (kf if est else self.blend.combine(new, kf, t, line)[0])
                    self._preds.setdefault((trip, i), []).append(
                        (t, t + old, t + new, None if kf is None else t + kf, bl, line, ageb))
                n += 1
                if n >= self.ETA_AHEAD:
                    break
        live = set(self.tracks)
        for k in [k for k in self._preds if k[0] not in live]:
            del self._preds[k]
        for k in [k for k in self._pred_at if k not in live]:
            del self._pred_at[k]

    def prune_routes(self, t):
        """Drop variants no bus ran on for ROUTE_UNUSED_S, and their trips."""
        with self.lock:
            busy = {tr.variant.key for tr in self.tracks.values()}
            gone = [k for k in self.variants if k not in busy and t - self.used.get(k, t) > ROUTE_UNUSED_S]
            for k in gone:
                var = self.variants.pop(k)
                self.checked.pop(k, None)
                self.used.pop(k, None)
                n = 0
                for trip in [x for x, v in self.race_variant.items() if v == k]:
                    del self.race_variant[trip]
                    n += 1
                log.info("dropped unused variant %s (%s -> %s, %d trips)", k, var.stops[0]["name"],
                         var.stops[-1]["name"], n)
            if gone:
                self._stop_index = (None, {})
                self.routes_version += 1
                self._save_cache()
            self._rechecked = {k: v for k, v in self._rechecked.items() if t - v < ROUTE_CHECK_EVERY}

    def refresh_stops(self, t):
        """Download the operator's stop list when due (weekly, or sooner while a route
        stop has no code), merge it into the stop db, and match code-less route stops.
        Returns True if it was downloaded."""
        with self.lock:
            unknown = any(not st["code"] for v in self.variants.values() for st in v.stops)
        last = self.refresh.get("stops", 0)
        wait = STOPS_RETRY if not self.refresh.get("stops_ok", True) else \
            (STOPS_SOON if unknown else STOPS_EVERY)
        if t - last < wait:
            return False
        self.refresh["stops"] = t
        try:
            req = urllib.request.Request(STOPS_URL, headers={"User-Agent": "NoSePol/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                feats = json.loads(r.read())["features"]
            ok = True
        except Exception as e:
            log.warning("stop list download: %s", e)
            feats, ok = [], False
        self.refresh["stops_ok"] = ok
        if ok:
            with self.lock:
                added, moved, missing = self.stopdb.merge(feats)
                matched = sum(v.refresh_codes(self.stopdb) for v in self.variants.values())
                if added or moved:
                    self.stopdb.save()
                if matched:
                    self._stop_index = (None, {})
                    self._save_cache()
            log.info("stop list: %d stops, %d new, %d moved, %d not listed any more, %d route stops matched",
                     len(feats), added, moved, missing, matched)
        return ok

    def poll_set(self, behind=True):
        """The fixed stops of every variant and, with behind, the stop each tracked bus
        last passed: that one lists the bus following it, which may be hidden from
        the fixed stops (two buses between the same pair of them)."""
        with self.lock:
            codes = [c for v in self.variants.values() for c in v.poll_stops()]
            if behind:
                codes += [c for tr in self.tracks.values()
                          if not tr.layover and (c := tr.variant.stop_behind(tr.fixes[-1][1]))]
                codes += self._ahead_codes(time.time())
        return list(dict.fromkeys(codes))

    def _ahead_codes(self, t, n=2):
        """The next n coded stops ahead of every known bus and ghost: they list it."""
        codes = []
        objs = [(tr.variant, tr.predict(t)[0]) for tr in self.tracks.values() if not tr.layover]
        objs += [(g.variant, g.s(t)) for g in self.ghosts.values()]
        for var, s in objs:
            codes += [st["code"] for st in var.stops if st["code"] and st["s"] > s - AT_STOP_M][:n]
        return codes

    def target_set(self, t):
        """A lighter sweep: each variant's first stop (departures) and the two stops just
        ahead of every known bus and ghost (for a bus near the end, the last two: arrivals).
        Buses hidden behind another one and buses joining mid-route are left to the full
        sweeps."""
        with self.lock:
            codes = []
            for v in self.variants.values():
                codes += [st["code"] for st in v.stops if st["code"]][:1]
            codes += self._ahead_codes(t)
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
            return code, None

    def sweep(self, t, full=True):
        codes = self.poll_set() if full else self.target_set(t)
        self.kf_stats["full_sweeps" if full else "targeted_sweeps"] += 1
        listed, fetched = {}, {}
        at = {}  # trip -> [(stop code, row)]
        for code, rs in self.pool.map(self._fetch, codes):
            if rs is None:
                continue  # no answer: says nothing (and mustn't look like buses leaving)
            fetched[code] = rs
            for r in rs:
                if self.wants(r["Line"]):
                    trip = f"{r['Line']}:{r['Race']}"
                    listed.setdefault(trip, []).append(r)
                    at.setdefault(trip, []).append((code, r))
        runs = {trip: self._freshest(trip, rs) for trip, rs in listed.items()}
        updated, moved = 0, []
        for trip, r in runs.items():
            if not r.get("IsStarted") or not r.get("Latitude"):
                tr = self.tracks.get(trip)
                if tr is not None and not tr.layover and tr.tl.ready():
                    # listed, but its position is gone: the timetable carries it on
                    with self.lock:
                        if not tr.gps_lost:
                            log.info("%s: GPS gone, still listed: on the timetable now", trip)
                        tr.gps_lost = True
                        self._timetable_points(tr, at.get(trip, []), t)
                        self.seen[trip] = t
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
                    tr = self.tracks[trip] = Track(trip, var, self.history)
                    g = self.ghosts.pop(trip, None)
                    if g is not None:  # the ghost got its GPS back: keep what its timetable learned
                        tr.tl = g.tl if g.variant is var else tr.tl
                        self.kf_stats["ghosts_then_gps"] += 1
                        log.info("ghost %s now has GPS", trip)
                tr.meta = {"line": r["Line"], "vehicle": r["Vehicle"], "direction": r["Direction"],
                           "destination": r["Destination"], "note": r["Note"] or None,
                           "api_arrival": r["ArrivalTime"]}
                self._timetable_points(tr, at.get(trip, []), t, tr.fixes[-1][1] if tr.fixes else None)
                if tr.add_fix(t, r["Latitude"], r["Longitude"]):
                    updated += 1
                    moved.append(tr)
                else:
                    self._standing(tr, t)
                self.seen[trip] = t
        with self.lock:
            self._passages(t, fetched)
            # A bus at the end of its route that this sweep didn't list has finished its
            # trip: the operator drops it at once. Buses then usually wait there and start
            # the line's next run back (watched on lines 6, 8, 9), so keep it as "layover".
            for k, tr in self.tracks.items():
                end = self.at_end(tr)
                # Position from the timetable (GPS old, frozen or gone), and that puts it at
                # the end: stops it already passed may keep listing it at its last GPS spot,
                # so being listed doesn't mean running. (Not "frozen" alone: a bus estimated
                # at the end counts as at a terminus, where standing isn't taken as frozen.)
                est_end = end and (tr.hold_s is not None or tr.estimated(t))
                if est_end and tr.hold_s is None:
                    # at least at the stop before the end: at_end went by the clock a moment
                    # later than t, so predict(t) can fall just short and undo the hold
                    stops = tr.variant.stops
                    tr.hold_s = max(tr.predict(t)[0], stops[-2]["s"] if len(stops) >= 3 else tr.variant.shape.length)
                    log.info("%s: by its timetable at the end (no fresh GPS): waiting there", k)
                tr.layover = end and (self.seen.get(k, 0) < t or est_end)
                tr.blind = self.blind(tr, t)
            self._assign_next_runs(t, fetched)
            self._ghosts(t, at)
            self._set_refs()
            reasons = {}
            for k, ts in self.seen.items():
                tr = self.tracks.get(k)
                if tr is not None and tr.layover:
                    if t - ts > LAYOVER_TTL:
                        reasons[k] = "layover_timeout"
                elif tr is not None and tr.blind:
                    if t - ts > self.blind_ttl(tr):
                        reasons[k] = "blind_timeout"
                elif t - ts > (KF_TTL if tr is not None and tr.kalman() else TRIP_TTL):
                    reasons[k] = "not_reported"
            for k, tr in self.tracks.items():
                if tr.layover:
                    if self._moved_on(tr):
                        reasons[k] = "next_run"  # its vehicle started a newer trip
                elif (why := self._drop_reason(tr, t)):
                    reasons[k] = why
            for trip, why in reasons.items():
                self.seen.pop(trip, None)
                tr = self.tracks.pop(trip, None)
                if tr:
                    self._retire_tt(tr)
                    self.frozen[trip] = tr.fixes[-1][3:5]
                    self._record_drop(trip, tr, why, t)
            self.stats["sweeps"] += 1
            self.stats["last_sweep"] = {"t": t, "stops": len(codes), "trips": len(runs),
                                        "new_fixes": updated, "full": full}
        log.info("sweep%s: %d stops, %d trips, %d new fixes, %d ghosts", "" if full else " (targeted)",
                 len(codes), len(runs), updated, len(self.ghosts))
        with self.lock:
            self._score_etas(t)
        try:
            self._route_checks(t, moved)
        except Exception:
            log.exception("route checks failed")
        return updated

    # ---- timetable filter ---------------------------------------------
    def _timetable_points(self, obj, rows, t, s_hint=None):
        """Note the scheduled time each listing stop gives for this run."""
        if s_hint is None:
            s_hint = obj.tl.position(t)[0] if obj.tl.ready() and obj.tl.schedule() else 0.0
        for code, r in rows:
            ts = self._sched_ts(r.get("Time"))
            i = stop_index(obj.variant, code, s_hint - 300)
            if ts is not None and i is not None:
                obj.tl.tt.add(i, ts)

    def _standing(self, tr, t):
        """Listed again at the same position. Standing at lights or a stop is news for the
        filter (its delay grows); standing this long mid-route more likely means the GPS
        froze, so from then on the timetable moves it."""
        still = tr.stopped_for(t)
        if (still is not None and still >= FROZEN_AFTER_S and not self.at_terminus(tr)
                and tr.tl.ready() and ESTIMATOR == "kalman"):
            if not tr.gps_frozen:
                self.kf_stats["frozen"] += 1
                log.info("%s: same position for %ds mid-route: GPS frozen? on the timetable now", tr.trip, still)
            tr.gps_frozen = True
            return
        tr.stand(t)

    def _passages(self, t, fetched):
        """A stop that listed a run last time and now lists another of the same line and
        direction instead: the run went by in between. A measurement with no GPS at all,
        used for ghosts and buses whose GPS is old, frozen or gone."""
        for code, rs in fetched.items():
            now_set = {f"{r['Line']}:{r['Race']}": (r["Line"], r["Direction"]) for r in rs if self.wants(r["Line"])}
            prev = self._listing.get(code)
            self._listing[code] = (t, now_set)
            if not prev or t - prev[0] > PASSAGE_GAP_S:
                continue
            served = set(now_set.values())
            for trip, ld in prev[1].items():
                if trip in now_set or ld not in served:
                    continue
                obj = self.tracks.get(trip) or self.ghosts.get(trip)
                if obj is None or obj.layover or not obj.tl.ready():
                    continue
                if isinstance(obj, Track) and obj.fixes[-1][0] >= prev[0] and not obj.gps_frozen:
                    continue  # fresh GPS knows better
                s = obj.tl.position(t)[0]
                i = stop_index(obj.variant, code, s - PASSAGE_NEAR_M)
                if i is None or abs(obj.variant.stops[i]["s"] - s) > PASSAGE_NEAR_M:
                    continue
                z = obj.tl.measure_passage(i, prev[0], t)
                if z is None:
                    continue
                self.kf_stats["passages"] += 1
                self.kf_stats["passage_z"] = (self.kf_stats["passage_z"] + [round(z)])[-50:]
                log.info("passage %s at %s (%s): delay %+ds%s", trip, code, pretty(obj.variant.stops[i]["name"]),
                         z, " [ghost]" if isinstance(obj, Ghost) else "")

    def _ghosts(self, t, at):
        """Runs listed with no GPS whose time at a stop listing them is due or past, that
        aren't still waiting at their first stop and no known bus is about to take: out
        running without GPS. Each becomes a Ghost, placed by its timetable."""
        paired = {tr.next_run["trip"] for tr in self.tracks.values() if tr.next_run}
        for trip, rows in at.items():
            if trip in self.tracks or any(r.get("IsStarted") and r.get("Latitude") for _, r in rows):
                continue
            live = [(c, r) for c, r in rows if not r.get("IsDestination") and self._sched_ts(r.get("Time"))]
            g = self.ghosts.get(trip)
            if g is None:
                if not live or trip in paired or any(r.get("TransitType") == "DepartureFromStop" for _, r in rows):
                    continue
                if min(self._sched_ts(r["Time"]) for _, r in live) > t + GHOST_DUE_S:
                    continue
                key = self.race_variant.get(trip)
                var = self.variants.get(key) if key else None
                if var is None:
                    continue  # its shape isn't known yet: fetched once it runs with GPS
                g = self.ghosts[trip] = Ghost(trip, var, t)
                self.kf_stats["ghosts_found"] += 1
                r0 = rows[0][1]
                log.info("ghost %s (%s -> %s): listed at %d stops with no GPS", trip, r0["Line"],
                         pretty(r0.get("Destination")), len(rows))
            r0 = rows[0][1]
            g.meta = {"line": r0["Line"], "vehicle": r0.get("Vehicle") or None, "direction": r0.get("Direction"),
                      "destination": r0.get("Destination"), "note": r0.get("Note") or None,
                      "api_arrival": r0.get("ArrivalTime")}
            g.last_listed = t
            self._timetable_points(g, rows, t, g.s(t) if g.tl.tt.at else 0.0)
            for c, r in live:  # still listed there: not past it yet
                i = stop_index(g.variant, c, g.s(t) - 300)
                if i is not None:
                    g.tl.not_past(i, t)
        for trip, g in list(self.ghosts.items()):
            sch = g.tl.schedule()
            done = sch and g.s(t) >= g.variant.shape.length - 1
            if trip in self.tracks or t - g.last_listed > GHOST_TTL or done:
                self._retire_tt(g)
                del self.ghosts[trip]
                log.info("ghost %s gone (%s), %d passages", trip,
                         "has GPS" if trip in self.tracks else ("reached the end" if done else "not listed"),
                         g.tl.passages)
        self.pending = [d for d in self.pending if d["trip"] not in self.ghosts]

    def _retire_tt(self, obj):
        if len(obj.tl.tt.at) >= 3:
            q = self._past_tt.setdefault(obj.variant.key, collections.deque(maxlen=6))
            q.append(obj.tl.tt)

    def _set_refs(self):
        """Give each trip another trip's timetable on the same route to extend its own
        past the stops it was listed at (a stop lists only the next bus, so a bus's
        times are known up to the bus ahead of it)."""
        by = collections.defaultdict(list)
        for o in list(self.tracks.values()) + list(self.ghosts.values()):
            by[o.variant.key].append(o.tl.tt)
        for o in list(self.tracks.values()) + list(self.ghosts.values()):
            mine = o.tl.tt.at
            if not mine:
                continue
            lo, hi = min(mine), max(mine)
            best, score = None, 0
            for tt in by[o.variant.key] + list(self._past_tt.get(o.variant.key, ())):
                if tt is o.tl.tt or not any(i in tt.at for i in mine):
                    continue
                sc = sum(1 for i in tt.at if i > hi or i < lo)
                if sc > score:
                    best, score = tt, sc
            o.tl.ref = best

    def _batch_seen(self, t, interval=False):
        """Record new GPS positions at t. Only the heartbeat times batches closely
        enough to count towards batch_interval; a forced sweep just keeps last_batch
        from going stale while the heartbeat looks for a stop that shows a bus."""
        lb = self.stats["last_batch"]
        if interval and lb:
            self.stats["batch_intervals"] = (self.stats["batch_intervals"] + [round(t - lb)])[-20:]
        self.stats["last_batch"] = t

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

    @staticmethod
    def blind(tr, t):
        """By its speed, past the last coded stop, on an end stretch where no stop has a
        code. Its last fix is always before that stop (the last one that can list it), so
        this goes by where it should be now: no report there says nothing about it."""
        b = tr.variant.blind_from
        if b is None or tr.layover:
            return False
        a, v, _, t_ref, _ = tr.cruise()
        return bool(v) and tr.advance(a, t - t_ref, v) > b + AT_STOP_M

    @staticmethod
    def blind_ttl(tr):
        """How long to keep a bus on such a stretch unreported: time to drive the rest
        (at its speed, at least BLIND_SPEED) plus a layover at the end."""
        v = tr.fit()[1] or 0.0
        left = tr.variant.shape.length - tr.fixes[-1][1]
        return min(BLIND_MAX_S, left / max(v, BLIND_SPEED) + LAYOVER_TTL)

    @staticmethod
    def at_end(tr):
        """At or past the second-to-last stop (buses often wait at the stop before the
        terminus), or within TERMINUS_M of the end of the route. With GPS frozen or gone,
        where its timetable has it."""
        s, stops = tr.fixes[-1][1], tr.variant.stops
        if tr.hold_s is not None:
            s = max(s, tr.hold_s)
        elif tr.estimated(time.time()):
            s = max(s, tr.predict(time.time())[0])
        if s > tr.variant.shape.length - TERMINUS_M:
            return True
        return len(stops) >= 3 and s >= stops[-2]["s"] - AT_STOP_M

    @classmethod
    def at_terminus(cls, tr):
        """At the end (see at_end), or at the start: within TERMINUS_M of the first stop or
        not yet past the second one (buses wait at the stand before departing)."""
        s, stops = tr.fixes[-1][1], tr.variant.stops
        if cls.at_end(tr) or s < stops[0]["s"] + TERMINUS_M:
            return True
        return len(stops) >= 3 and s <= stops[1]["s"] + AT_STOP_M

    def stop_kind(self, tr, now):
        """'layover' (trip over, waiting at the terminus for its next run), 'terminus'
        (stopped there, still listed), 'midroute' (may have a problem), or None if moving."""
        if tr.layover:
            return "layover"
        if tr.stopped_for(now) is None or tr.gps_frozen:
            return None
        return "terminus" if self.at_terminus(tr) else "midroute"

    def is_stale(self, tr, now):
        """GPS old and nothing better to go by (with a timetable it is "estimated" instead)."""
        return (now - tr.fixes[-1][0] > STALE_S and not tr.layover and not tr.blind
                and tr.stopped_for(now) is None and not tr.estimated(now))

    def _drop_reason(self, tr, t):
        """Why a bus that stopped moving should go now, or None to keep it. It stays
        visible, marked, up to TERMINUS_TTL waiting at a terminus and up to STOPPED_TTL
        mid-route; sooner (TRIP_TTL) once its vehicle runs a newer trip, since then
        this one is a leftover."""
        still = t - tr.fixes[-1][0]
        if tr.estimated(t) and tr.predict(t)[0] >= tr.variant.shape.length - 1:
            # No GPS for a while, and by its timetable the trip is over: it waits at the
            # end like any finished bus (gone when its vehicle starts a newer run, or after
            # LAYOVER_TTL), rather than being dropped and missing its next run.
            if tr.hold_s is None:
                tr.hold_s = tr.variant.shape.length
                log.info("%s: by its timetable at the end (drop check): waiting there", tr.trip)
            tr.layover = True
            return None
        if self.at_terminus(tr):
            if still > TERMINUS_TTL:
                return "terminus_timeout"
        elif still > STOPPED_TTL:
            return "midroute_timeout"
        if still <= TRIP_TTL:
            return None
        return "vehicle_reused" if self._moved_on(tr) else None

    def _moved_on(self, tr):
        """Its vehicle has a newer trip: this one is over."""
        veh = tr.meta.get("vehicle")
        return bool(veh) and any(o is not tr and o.meta.get("vehicle") == veh and o.fixes[-1][0] > tr.fixes[-1][0]
                                 for o in self.tracks.values())

    @staticmethod
    def _sched_ts(text):
        try:
            return datetime.fromisoformat(text).replace(tzinfo=LOCAL_TZ).timestamp()
        except (TypeError, ValueError):
            return None

    def _assign_next_runs(self, t, fetched):
        """Pair each line's next unstarted departure listed at a terminus with the bus that
        will probably run it: first buses already waiting there (in arrival order, as
        observed), then buses still heading there that arrive in time (nearest first).
        A departure no known bus can take is kept in self.pending: its bus is out of
        sight (depot, a missed arrival, a server restart). The feed lists one next run
        per line and direction at a stop, so there is at most one departure to pair."""
        prev = {tr.trip: tr.next_run["trip"] for tr in self.tracks.values() if tr.next_run}
        cands = {}
        for tr in self.tracks.values():
            tr.next_run = None
            code = tr.variant.stops[-1]["code"]
            if not code:
                continue
            key = (code, tr.variant.line.rstrip("/"))
            if self.at_end(tr) and (tr.layover or tr.stopped_for(t) is not None):
                cands.setdefault(key, []).append((0, tr.fixes[-1][0], tr, t))  # waiting: ready now
            else:
                s_, v, *_ = tr.predict(t)
                left = tr.variant.shape.length - s_
                eta = tr.eta(s_, tr.variant.shape.length, v, t)[0] if tr.stopped_for(t) is None else None
                eta = eta if eta is not None else (0 if left < 500 else None)
                if eta is not None:
                    cands.setdefault(key, []).append((1, left, tr, t + eta))
        # Trips start only where a variant starts. A short trip's end (Via Battisti,
        # Via Svevo, Barcola) is mid-route for the full trips: it lists the next one not
        # yet started upstream, at its time there, which is not a departure from there.
        starts = {}
        for var in self.variants.values():
            st = var.stops[0]
            if st["code"]:
                starts.setdefault((st["code"], var.line.rstrip("/")), st)
        first = {}  # trip -> (code, line, run) at its earliest listed start: one place per trip
        for (code, line), st in starts.items():
            for r in fetched.get(code) or []:
                if (r["Line"].rstrip("/") == line and not r.get("IsDestination")
                        and not r.get("IsStarted") and r.get("Time")):
                    trip = f"{r['Line']}:{r['Race']}"
                    if trip not in first or r["Time"] < first[trip][2]["Time"]:
                        first[trip] = (code, line, r)
        deps_at = {}
        for code, line, r in first.values():
            deps_at.setdefault((code, line), []).append(r)
        pending = []
        for (code, line), st in starts.items():
            deps = sorted(deps_at.get((code, line), []), key=lambda r: r["Time"])
            cs = sorted(cands.get((code, line), []), key=lambda c: (c[0], c[1]))
            for r in deps:
                when = self._sched_ts(r["Time"])
                run = {"trip": f"{r['Line']}:{r['Race']}", "destination": pretty(r.get("Destination")),
                       "scheduled": r["Time"][11:16], "stop": code}
                # a bus already paired with this run keeps it unless clearly behind (no flicker)
                grace = lambda c: LATE_OK * (2 if prev.get(c[2].trip) == run["trip"] else 1)
                pick = next((c for c in cs if c[0] == 0 or (when is not None and c[3] <= when + grace(c))), None)
                if pick:
                    cs.remove(pick)
                    pick[2].next_run = run
                elif when is not None and when - t <= PENDING_AHEAD and t < when + PENDING_LATE:
                    pending.append({**run, "line": line, "when": when, "stop_name": pretty(st["name"]),
                                    "lat": st["lat"], "lon": st["lon"], "seen": t})
        # Keep a pending departure that dropped out of the listing (it started, or one
        # answer missed it) until its bus shows up with GPS, a known bus takes it, or
        # PENDING_LATE after its time: no gap between "about to start" and the real bus.
        now_ids = {d["trip"] for d in pending}
        paired = {tr.next_run["trip"] for tr in self.tracks.values() if tr.next_run}
        for d in self.pending:
            if (d["trip"] not in now_ids and d["trip"] not in self.tracks and d["trip"] not in paired
                    and t < d["when"] + PENDING_LATE):
                pending.append(d)
        self.pending = pending

    def _record_drop(self, trip, tr, reason, t):
        s = tr.fixes[-1][1]
        last = max((st for st in tr.variant.stops if st["s"] <= s + AT_STOP_M), key=lambda st: st["s"],
                   default=tr.variant.stops[0])
        d = {"t": round(t), "trip": trip, "line": tr.variant.line.rstrip("/"),
             "destination": pretty(tr.meta.get("destination") or tr.variant.destination),
             "vehicle": tr.meta.get("vehicle"), "reason": reason,
             "still_s": round(t - tr.fixes[-1][0]), "s": round(s), "length": round(tr.variant.shape.length),
             "near": pretty(last["name"])}
        self.drops.append(d)
        self.drop_counts[reason] = self.drop_counts.get(reason, 0) + 1
        log.info("drop %s (%s -> %s) %s: still %ds at %dm/%dm near %s", trip, d["line"], d["destination"],
                 reason, d["still_s"], d["s"], d["length"], d["near"])

    def fill_codes(self):
        """Look for the codes of route stops that have none, so they can be polled: probe
        the codes just past the last known one of each district near them (the first
        step of tools/fill_stops.py, which can search wider). New stops are saved to
        data/stops.json and matched to the routes. Returns the route stops matched."""
        with self.lock:
            targets = [FRAME.xy(st["lat"], st["lon"]) for v in self.variants.values()
                       for st in v.stops if not st["code"]]
        if not targets:
            return 0
        by = collections.defaultdict(list)
        for st in self.stopdb.stops:
            by[st["code"][:2]].append(st)
        near = {z: min(math.dist(st["xy"], p) for st in sts for p in targets) for z, sts in by.items()}
        zones = sorted((z for z in by if near[z] <= FILL_NEAR_M), key=near.get)
        known = {st["code"] for st in self.stopdb.stops}
        jobs = [code for z in zones for n in range(1, FILL_GAP + 1)
                if (code := f"{z}{max(int(st['code'][2:]) for st in by[z]) + n:03d}") not in known]
        log.info("%d route stops without a code: probing %d codes in districts %s",
                 len(targets), len(jobs), ",".join(zones))
        found = 0
        for code in jobs:
            try:
                self.stats["requests"] += 1
                d = get("info", StopCode=code)
            except Exception as e:
                log.warning("probe %s: %s", code, e)
                continue
            finally:
                time.sleep(0.2)
            if d and d.get("Latitude"):
                self.stopdb.add(code, d.get("Address"), d["Latitude"], d["Longitude"], d.get("IsUrban"))
                found += 1
        matched = 0
        if found:
            with self.lock:
                matched = sum(v.refresh_codes(self.stopdb) for v in self.variants.values())
                self._stop_index = (None, {})
            try:
                self.stopdb.save()
            except OSError as e:
                log.warning("saving stops: %s", e)
        left = len(targets) - matched
        log.info("stop codes: %d new stops, %d route stops matched, %d still without a code%s",
                 found, matched, left, " (run tools/fill_stops.py)" if left else "")
        return matched

    def _data_loop(self):
        """Housekeeping every DATA_EVERY: the stop list when due (probing codes only while
        it can't be downloaded), dropping unused routes, saving what was learned."""
        fill_at = 0
        while True:
            t = time.time()
            try:
                self.refresh_stops(t)
                if not self.refresh.get("stops_ok", True) and t - fill_at > FILL_EVERY:
                    fill_at = t
                    self.fill_codes()
            except Exception:
                log.exception("stop refresh failed")
            try:
                self.prune_routes(t)
                with self.lock:
                    self._save_refresh()
                    self.history.save()
                    self.blend.save()
            except Exception:
                log.exception("saving data failed")
            time.sleep(DATA_EVERY)

    def run(self):
        self.discover()
        threading.Thread(target=self._data_loop, daemon=True).start()
        last, last_sweep, hb = None, 0, None  # last: (stop, signature) the heartbeat saw
        n = 0
        while True:
            t = time.time()
            codes = self.poll_set(behind=False)
            sig = None  # (stop, positions it lists), if it lists a running bus
            if codes:
                # Watch one stop. A stop whose listed buses haven't started yet shows no
                # positions, so it can't reveal a batch: move on to the next stop then.
                if hb not in codes:
                    hb = codes[len(codes) // 2]
                code = hb
                _, rs = self._fetch(code)
                rs = rs or []
                pos = tuple(sorted((r["Race"], r["Latitude"], r["Longitude"]) for r in rs
                                   if r.get("IsStarted") and r.get("Latitude")))
                if pos:
                    sig = (code, pos)
                else:
                    hb = codes[(codes.index(code) + 1) % len(codes)]
            changed = bool(sig and last and last[0] == sig[0] and last[1] != sig[1])
            if changed:
                self._batch_seen(t, interval=True)
            if changed or t - last_sweep > FORCE_SWEEP_EVERY:
                full = not changed or n % FULL_EVERY == 0
                n += 1
                try:
                    if self.sweep(t, full) and not changed:
                        self._batch_seen(t)  # forced sweep found newer fixes
                except Exception:
                    log.exception("sweep failed")
                last_sweep = t
            last = sig
            time.sleep(max(0.0, HEARTBEAT_EVERY - (time.time() - t)))

    # ---- views -------------------------------------------------------
    def vehicles(self, line=None):
        now = time.time()
        with self.lock:
            out = [{**tr.state(now), "stop_kind": self.stop_kind(tr, now), "stale": self.is_stale(tr, now)}
                   for tr in self.tracks.values() if line in (None, tr.variant.line.rstrip("/"))]
            out += [{**g.state(now), "stop_kind": None, "stale": False}
                    for g in self.ghosts.values() if line in (None, g.variant.line.rstrip("/"))]
            pending = [{**{k: v for k, v in d.items() if k not in ("when", "seen")}, "in_s": round(d["when"] - now)}
                       for d in self.pending if line in (None, d["line"])]
        for v in out:
            v["destination"] = pretty(v.get("destination"))
        return {"t": now, "vehicles": sorted(out, key=lambda v: (v["line"], v["variant"], v["s"])),
                "pending": sorted(pending, key=lambda d: d["in_s"])}

    def live(self):
        """What /api/stream sends each STREAM_EVERY: the vehicles, the feed's last GPS
        batch, and a count of route variants (the page reloads routes when it changes).
        Built at most once a second, however many streams are open."""
        now = time.time()
        with self._live_lock:
            if self._live and now - self._live[0] < 1:
                return self._live[1]
            out = self.vehicles()
            with self.lock:
                ints = self.stats["batch_intervals"]
                out |= {"last_batch": self.stats["last_batch"],
                        "batch_interval": round(statistics.median(ints)) if ints else None,
                        "variants": self.routes_version}
            body = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            self._live = (now, body)
            return body

    def line_style(self, line):
        base = line.rstrip("/")
        i = self.lines.index(base) if base in self.lines else len(self.lines)
        color, ink = PALETTE[i % len(PALETTE)]
        return {"name": line_name(base), "color": color, "ink": ink}

    def lines_summary(self):
        with self.lock:
            return [{"line": l, **self.line_style(l),
                     "variants": [v.summary() for v in self.variants.values() if v.line.rstrip("/") == l],
                     "vehicles": sum(tr.variant.line.rstrip("/") == l for tr in self.tracks.values()),
                     "ghosts": sum(g.variant.line.rstrip("/") == l for g in self.ghosts.values()),
                     "pending": sum(d["line"] == l for d in self.pending)}
                    for l in self.lines]

    def line_track(self, line):
        """One line as two strips: per direction, its full route's stops and every bus
        on it placed by s along that route (short-turn trips mapped onto it), front
        first, each with the time the bus behind needs to reach where it is now."""
        now = time.time()
        with self.lock:
            vs = [v for v in self.variants.values() if v.line.rstrip("/") == line]
            if not vs:
                return None
            dirs = []
            for d in sorted({v.direction for v in vs}):
                mine = [v for v in vs if v.direction == d]
                main = max(mine, key=lambda v: (not v.line.endswith("/"), v.shape.length))
                buses = []
                for tr in list(self.tracks.values()) + list(self.ghosts.values()):
                    if tr.variant not in mine:
                        continue
                    st = tr.state(now)
                    ghost = isinstance(tr, Ghost)
                    kind = None if ghost else self.stop_kind(tr, now)
                    buses.append({k: st.get(k) for k in ("trip", "vehicle", "v", "age", "stopped_s", "next_run", "blind",
                                                         "estimated", "timetable")}
                                 | {"s": round(self._on_main(tr.variant, main, st), 1), "short": tr.variant is not main,
                                    "to": pretty(tr.variant.stops[-1]["name"]), "stop_kind": kind, "ghost": ghost,
                                    "stale": False if ghost else self.is_stale(tr, now)})
                buses.sort(key=lambda b: -b["s"])
                self._gaps(main, buses)
                starts = {v.stops[0]["code"] for v in mine}
                dirs.append({"direction": d, "key": main.key, "headsign": pretty(main.destination),
                             "from": pretty(main.stops[0]["name"]), "length": round(main.shape.length),
                             "stops": [{"code": s["code"], "name": pretty(s["name"]), "s": s["s"],
                                        "lat": s["lat"], "lon": s["lon"]} for s in main.stops],
                             "short_ends": sorted({pretty(v.stops[-1]["name"]) for v in mine if v is not main}),
                             "buses": buses,
                             "pending": [{"trip": p["trip"], "scheduled": p["scheduled"], "destination": p["destination"],
                                          "in_s": round(p["when"] - now)}
                                         for p in self.pending if p["line"] == line and p["stop"] in starts]})
        return {"t": now, "line": line, **self.line_style(line), "directions": dirs}

    @staticmethod
    def _on_main(var, main, st):
        """A bus's s on its variant, as s along the direction's full route."""
        if var is main:
            return st["s"]
        # anchor on the last stop it passed, found on the full route, so loops don't confuse it
        prev = next((x for x in reversed(var.stops) if x["s"] <= st["s"] + 1), var.stops[0])
        hint = next((x["s"] for x in main.stops if x["code"] and x["code"] == prev["code"]), None)
        return main.shape.project(st["lat"], st["lon"], hint)[1]

    GAP_V = 6.0  # m/s between stops, for a gap when the bus behind has no usable speed

    def _gaps(self, main, buses):
        """gap_s on each bus: seconds the next running bus behind it needs to get there.
        Buses waiting at a terminus or stale don't count as running."""
        run = [b for b in buses if b["stop_kind"] not in ("layover", "terminus") and not b["stale"]]
        for front, back in zip(run, run[1:]):
            v = back["v"] if back["v"] and back["v"] > 1 else self.GAP_V
            k = sum(1 for x in main.stops if back["s"] + 1 < x["s"] < front["s"] - 1)
            front["gap_s"] = round((front["s"] - back["s"]) / v + k * DWELL_S)

    # ---- stops ---------------------------------------------------------
    def stop_index(self):
        """Every coded stop on a tracked route, with the lines and directions serving it."""
        with self.lock:
            keys = tuple(sorted(self.variants))
            if self._stop_index[0] == keys:
                return self._stop_index[1]
            idx = {}
            for v in self.variants.values():
                for i, st in enumerate(v.stops):
                    if not st["code"]:
                        continue
                    e = idx.setdefault(st["code"], {
                        "code": st["code"], "name": pretty(st["name"]), "lat": st["lat"],
                        "lon": st["lon"], "xy": FRAME.xy(st["lat"], st["lon"]), "serves": {}})
                    if i < len(v.stops) - 1:  # a route's last stop gets arrivals, not departures
                        e["serves"].setdefault((v.line.rstrip("/"), v.direction), v.destination)
            for e in idx.values():
                e["key"] = fold(f"{e['name']} {e['code']}")
                e["lines"] = sorted({l for l, _ in e["serves"]}, key=self._line_order)
            self._stop_index = (keys, idx)
            return idx

    def _line_order(self, line):
        return (self.lines.index(line) if line in self.lines else 99, line)

    def _stop_public(self, e, dist=None):
        out = {"code": e["code"], "name": e["name"], "lat": e["lat"], "lon": e["lon"],
               "lines": e["lines"],
               "directions": [{"line": l, "direction": d, "destination": pretty(dest)}
                              for (l, d), dest in sorted(e["serves"].items(),
                                                         key=lambda kv: self._line_order(kv[0][0]))]}
        if dist is not None:
            out["dist"] = round(dist)
        return out

    def stops(self, q=None, near=None, limit=20):
        """Search by words of the name or code, and/or sort by distance from near=(lat, lon)."""
        items = list(self.stop_index().values())
        words = fold(q).split() if q else []
        if words:
            items = [e for e in items if all(w in e["key"] for w in words)]
        dist = {}
        if near:
            p = FRAME.xy(*near)
            dist = {e["code"]: math.dist(p, e["xy"]) for e in items}
            items.sort(key=lambda e: dist[e["code"]])
        elif words:  # names that start with the first word first
            items.sort(key=lambda e: (not e["key"].startswith(words[0]), e["name"]))
        else:
            items.sort(key=lambda e: e["name"])
        return [self._stop_public(e, dist.get(e["code"])) for e in items[:limit]]

    def operator_runs(self, code):
        """The operator's own next runs at a stop, cached briefly; None if unreachable."""
        t, runs = self._operator.get(code, (0, None))
        if time.time() - t > OPERATOR_CACHE_S:
            try:
                self.stats["requests"] += 1
                runs = [r for r in get("mrcruns", StopCode=code, IsUrban="true") or []
                        if self.wants(r["Line"])]
                self._operator[code] = (time.time(), runs)
            except Exception as e:
                log.warning("operator runs %s: %s", code, e)
        return runs

    def stop_detail(self, code):
        """A stop with every tracked bus still heading to it, our ETA beside the operator's."""
        e = self.stop_index().get(code)
        if not e:
            return None
        runs = self.operator_runs(code)  # network: outside the lock
        now = time.time()
        deps = {}
        with self.lock:
            for trip, tr in self.tracks.items():
                s, v, _, _ = tr.predict(now)
                stops = tr.variant.stops
                st = next((x for x in stops[:-1] if x["code"] == code and x["s"] > s - AT_STOP_M), None)
                if not st:
                    continue
                stopped = tr.stopped_for(now)
                dist = max(0.0, st["s"] - s)
                eta, src = tr.eta(s, st["s"], v, now) if stopped is None else (None, None)
                eta = None if eta is None else round(eta)
                age = round(now - tr.fixes[-1][0])
                deps[trip] = {"trip": trip, "line": tr.variant.line.rstrip("/"),
                              "destination": pretty(tr.meta.get("destination") or tr.variant.destination),
                              "direction": tr.variant.direction, "live": True, "eta_s": eta,
                              "eta_src": src if eta is not None else None,
                              "dist": round(dist), "v": None if v is None else round(v, 2),
                              "stopped_s": None if stopped is None else round(stopped),
                              "stop_kind": self.stop_kind(tr, now),
                              "stale": self.is_stale(tr, now), "age": age, "estimated": tr.estimated(now),
                              "operator": None, "operator_eta_s": None}
            for trip, g in self.ghosts.items():
                s = g.s(now)
                st = next((x for x in g.variant.stops[:-1] if x["code"] == code and x["s"] > s - AT_STOP_M), None)
                if not st:
                    continue
                eta = g.tl.eta(st["s"], now)
                deps[trip] = {"trip": trip, "line": g.variant.line.rstrip("/"),
                              "destination": pretty(g.meta.get("destination") or g.variant.destination),
                              "direction": g.variant.direction, "live": False, "ghost": True,
                              "eta_s": None if eta is None else round(eta), "eta_src": "timetable",
                              "dist": round(max(0.0, st["s"] - s)), "v": None, "stopped_s": None,
                              "stop_kind": None, "stale": False, "age": None, "estimated": True,
                              "operator": None, "operator_eta_s": None}
        later = []
        for r in runs or []:
            trip = f"{r['Line']}:{r['Race']}"
            op = {"operator": r.get("ArrivalTime"), "operator_eta_s": operator_eta(r.get("ArrivalTime")),
                  "scheduled": (r.get("Time") or "")[11:16] or None}
            if trip in deps:
                deps[trip].update(op)
            elif not r.get("IsDestination"):
                deps[trip] = {"trip": trip, "line": r["Line"].rstrip("/"),
                              "destination": pretty(r.get("Destination")),
                              "direction": r.get("Direction"), "live": False, "eta_s": None, "eta_src": None,
                              "dist": None, "v": None, "stopped_s": None, "stop_kind": None, "stale": False,
                              "age": None, **op}
            if r.get("NextPasses"):
                later.append({"line": r["Line"].rstrip("/"), "destination": pretty(r.get("Destination")),
                              "times": [x.strip() for x in r["NextPasses"].split("-") if x.strip()]})

        with self.lock:  # a departure from here that a bus waiting at this terminus will probably take
            for tr in self.tracks.values():
                nr = tr.next_run
                waiting = tr.layover or tr.stopped_for(now) is not None
                if waiting and nr and nr["stop"] == code and nr["trip"] in deps and not deps[nr["trip"]]["live"]:
                    deps[nr["trip"]]["waiting_bus"] = {"trip": tr.trip, "vehicle": tr.meta.get("vehicle"),
                                                       "waiting_s": round(now - tr.fixes[-1][0])}

        def order(d):
            eta = d["eta_s"] if d["eta_s"] is not None else d["operator_eta_s"]
            return (eta is None, eta or 0, d["dist"] or 0)

        return {"t": now, **self._stop_public(e), "operator_ok": runs is not None,
                "departures": sorted(deps.values(), key=order), "later": later}

    # ---- one trip ------------------------------------------------------
    def trip_detail(self, trip):
        now = time.time()
        with self.lock:
            tr = self.tracks.get(trip)
            if not tr and (g := self.ghosts.get(trip)):
                st = g.state(now)
                ahead = []
                for x in g.variant.stops:
                    if x["s"] > st["s"] + 5:
                        eta = g.tl.eta(x["s"], now)
                        ahead.append({"code": x["code"], "name": pretty(x["name"]), "dist": round(x["s"] - st["s"]),
                                      "eta_s": None if eta is None else round(eta), "eta_src": "timetable"})
                return {"t": now, **st, "destination": pretty(st.get("destination")), "stop_kind": None,
                        "line": g.variant.line.rstrip("/"), "stale": False,
                        "from": pretty(g.variant.stops[0]["name"]), "to": pretty(g.variant.stops[-1]["name"]),
                        "stops_ahead": ahead, "fixes": []}
            if not tr:
                return None
            st = tr.state(now)
            s, v, stopped = st["s"], st["v"], st["stopped_s"]
            ahead = []
            for x in tr.variant.stops:
                if x["s"] <= s + 5:
                    continue
                dist = x["s"] - s
                eta, src = tr.eta(s, x["s"], v, now) if stopped is None else (None, None)
                ahead.append({"code": x["code"], "name": pretty(x["name"]), "dist": round(dist),
                              "eta_s": None if eta is None else round(eta),
                              "eta_src": src if eta is not None else None})
            fixes = [[round(t - now), round(fs)] for t, fs, *_ in tr.fixes]
        return {"t": now, **st, "destination": pretty(st.get("destination")),
                "stop_kind": self.stop_kind(tr, now),
                "line": tr.variant.line.rstrip("/"), "stale": self.is_stale(tr, now),
                "from": pretty(tr.variant.stops[0]["name"]), "to": pretty(tr.variant.stops[-1]["name"]),
                "stops_ahead": ahead, "fixes": fixes}

    # ---- feed health ---------------------------------------------------
    def status(self):
        now = time.time()
        with self.lock:
            states = [{**tr.state(now), "stop_kind": self.stop_kind(tr, now), "stale": self.is_stale(tr, now)}
                      for tr in self.tracks.values()]
            ints = self.stats["batch_intervals"]
            lb = self.stats["last_batch"]
            unknown = sum(1 for v in self.variants.values() for st in v.stops if not st["code"])
            return {"t": now, "lines": self.lines,
                    "last_batch_age": None if lb is None else round(now - lb),
                    "batch_interval": round(statistics.median(ints)) if ints else None,
                    "buses": len(states),
                    "stopped": sum(1 for x in states if x["stopped_s"]),
                    "stopped_midroute": sum(1 for x in states if x["stop_kind"] == "midroute"),
                    "at_terminus": sum(1 for x in states if x["stop_kind"] == "terminus"),
                    "layover": sum(1 for x in states if x["stop_kind"] == "layover"),
                    "stale": sum(1 for x in states if x["stale"]),
                    "estimated": sum(1 for x in states if x["estimated"]),
                    "estimator": ESTIMATOR,
                    "timetable": {**{k: v for k, v in self.kf_stats.items() if k != "passage_z"},
                                  "ghosts_now": len(self.ghosts),
                                  "on_timetable": sum(1 for tr in self.tracks.values() if tr.tl.ready()),
                                  "passage_delay_recent_s": self.kf_stats["passage_z"][-20:],
                                  "full_every": FULL_EVERY},
                    "requests": self.stats["requests"],
                    "requests_per_hour": round(self.stats["requests"] / max(now - self.started, 1) * 3600),
                    "variants": len(self.variants), "stops_unknown_code": unknown,
                    "uptime_s": round(now - self.started),
                    "drops_by_reason": dict(self.drop_counts),
                    "drops_recent": [{**d, "ago_s": round(now - d["t"])} for d in reversed(self.drops)][:20],
                    "resync_suggested": unknown > 0,
                    "stop_list": {"downloaded": self.refresh.get("stops"), "ok": self.refresh.get("stops_ok")},
                    "route_checks_last_hour": len(self._recheck_times),
                    "history": self.history.summary(),
                    "blend_weights": self.blend.summary(now, self.lines),
                    "eta_errors": {h: {"n": e["n"], "speed_mae_s": round(e["speed"] / e["n"]),
                                       "ours_mae_s": round(e["ours"] / e["n"]),
                                       # the timetable filter's, and ours on the same arrivals
                                       "n_kalman": e.get("nk", 0),
                                       "kalman_mae_s": round(e["kalman"] / e["nk"]) if e.get("nk") else None,
                                       "ours_same_mae_s": round(e["ours_k"] / e["nk"]) if e.get("nk") else None,
                                       # the two combined, weighted by time to the stop
                                       "blend_mae_s": round(e.get("blend", 0) / e["nk"]) if e.get("nk") else None}
                                   for h, e in sorted(self.eta_errors.items()) if e["n"]},
                    # the same arrivals for all four (those with a timetable ETA), recent ones:
                    # mean = how good it usually is, p95 = how bad it gets
                    "eta_spread": self._eta_spread(),
                    # the same split by line, and by how old the bus's GPS was when predicted
                    "eta_by_line": self._eta_spread("line"),
                    "eta_by_gps_age": self._eta_spread("gps_age")}

    def _eta_spread(self, by=None):
        """Mean, median and p95 of each method's absolute error, per horizon, and with by
        ("line" or "gps_age") per horizon and group: {horizon: {group: stats}}."""
        def stats(rows):
            out = {"n": len(rows)}
            for j, name in enumerate(("speed", "history", "timetable", "blend"), 3):
                xs = sorted(r[j] for r in rows)
                q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
                out[name] = {"mean": round(sum(xs) / len(xs)), "median": round(q(0.5)), "p95": round(q(0.95))}
            return out
        col = {None: None, "line": 1, "gps_age": 2}[by]
        groups = {}
        for r in self.eta_samples:
            g = groups.setdefault(r[0], {})
            g.setdefault(None if col is None else r[col], []).append(r)
        if col is None:
            return {h: stats(g[None]) for h, g in sorted(groups.items())}
        order = [name for _, name in self.GPS_AGE_BANDS] if by == "gps_age" else self.lines
        return {h: {k: stats(g[k]) for k in sorted(g, key=lambda k: order.index(k) if k in order else 99)}
                for h, g in sorted(groups.items())}

    def shapes(self, line):
        with self.lock:
            return [v.geojson() for v in self.variants.values() if line in (None, v.line.rstrip("/"))]


def make_handler(tracker):
    streams = {"open": 0}
    streams_lock = threading.Lock()

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(WEB), **kw)

        def log_message(self, *a):
            pass

        def end_headers(self):
            # Without this, browsers reuse a cached page after an update; "no-cache"
            # makes them revalidate each time (a cheap 304 when nothing changed).
            if not self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            """Server-sent events: tracker.live() every STREAM_EVERY s, so the page needn't
            poll. Closes after STREAM_MAX_S; EventSource reconnects by itself."""
            with streams_lock:
                if streams["open"] >= MAX_STREAMS:
                    return self._json({"error": "too many streams, poll /api/vehicles"}, 503)
                streams["open"] += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")  # proxies: don't hold events back
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(f"retry: {STREAM_EVERY * 1000}\n\n".encode())
                end = time.time() + STREAM_MAX_S
                while time.time() < end:
                    t = time.time()
                    self.wfile.write(b"data: " + tracker.live().encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(max(0.0, STREAM_EVERY - (time.time() - t)))
            except OSError:  # the page went away
                pass
            finally:
                with streams_lock:
                    streams["open"] -= 1
                self.close_connection = True

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
            if not parts or parts[0] != "api":
                return super().do_GET()
            qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            match parts[1:]:
                case ["stream"]:
                    return self._stream()
                case ["live"]:  # one /api/stream update, for pages that can't stream
                    body = tracker.live().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                case ["stops"]:
                    try:
                        near = tuple(float(x) for x in qs["near"].split(",")) if "near" in qs else None
                        if near is not None and len(near) != 2:
                            raise ValueError
                        limit = max(1, min(100, int(qs.get("limit", 20))))
                    except ValueError:
                        return self._json({"error": "near=lat,lon and limit=N"}, 400)
                    return self._json(tracker.stops(qs.get("q"), near, limit))
                case ["stops", code]:
                    d = tracker.stop_detail(code)
                    return self._json(d) if d else self._json({"error": "unknown stop"}, 404)
                case ["trips", trip]:
                    d = tracker.trip_detail(trip)
                    return self._json(d) if d else self._json({"error": "trip not tracked"}, 404)
                case ["status"]:
                    return self._json(tracker.status())
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
                case ["lines", line, "track"]:
                    d = tracker.line_track(line)
                    return self._json(d) if d else self._json({"error": "unknown line"}, 404)
                case ["debug"]:
                    with tracker.lock:
                        return self._json({**tracker.stats, "poll_set": tracker.poll_set(),
                                           "tracks": {k: [f[:3] for f in tr.fixes]
                                                      for k, tr in tracker.tracks.items()},
                                           "timetables": {k: {"at": tr.tl.tt.at, "kf": tr.tl.kf and tr.tl.kf.x,
                                                              "last": tr.tl.kf and tr.tl.kf.last}
                                                          for k, tr in list(tracker.tracks.items())
                                                          + list(tracker.ghosts.items())}})
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
