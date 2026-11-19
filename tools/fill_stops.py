#!/usr/bin/env python3
"""Fill gaps in data/stops.json for stops on the cached routes (data/routes.json).

scan_stops.py stops a district after 15 consecutive misses, which loses stops
after a numbering gap and whole districts whose numbering starts late. This
probes, in priority order, until every route stop matches a code:
  1. districts 00-09 if unscanned (city centre; codes start at 00001)
  2. past the last known code of districts near an unmatched route stop
  3. districts 10-99 with no known stops, over a wider range
  4. past the last known code of every other district
Run with the server stopped or restart it afterwards.
"""
import json
import math
import pathlib
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

API = "https://realtime.tplfvg.it/API/v1.0/polemonitor"
DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
MATCH_M = 40
NEAR_M = 2000
GAP = 60            # codes probed past a district's last known code
EMPTY_RANGE = 200   # codes probed in districts with nothing known
THREADS = 3
PAUSE = 0.1

K = math.radians(1) * 6371000


def dist(a, b):
    return math.hypot((a[1] - b[1]) * K * math.cos(math.radians(45.65)), (a[0] - b[0]) * K)


def info(code):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{API}/info?StopCode={code}", timeout=10) as r:
                return json.loads(r.read() or b"null")
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def main():
    stops = json.loads((DATA / "stops.json").read_text())
    routes = json.loads((DATA / "routes.json").read_text())
    points = {}
    for v in routes["variants"]:
        segs = v["segments"]
        for lon, lat in [s[0] for s in segs] + [segs[-1][-1]]:
            points[(round(lat, 6), round(lon, 6))] = v["line"]
    known = [(s["lat"], s["lon"]) for s in stops.values()]
    targets = [p for p in points if not any(dist(p, k) <= MATCH_M for k in known)]
    print(f"{len(points)} route stops, {len(targets)} without a code", flush=True)
    if not targets:
        return

    by = defaultdict(list)
    for c, s in stops.items():
        by[c[:2]].append((int(c[2:]), (s["lat"], s["lon"])))
    near_d = lambda z: min(dist(p, t) for _, p in by[z] for t in targets)
    near = sorted((z for z in by if near_d(z) <= NEAR_M), key=near_d)
    far = sorted(set(by) - set(near), key=near_d)
    empty = [f"{z:02d}" for z in range(0, 100) if f"{z:02d}" not in by]
    # unscanned low districts (00-09, the city centre) first, then gaps near targets
    jobs = ([f"{z}{n:03d}" for z in empty if z < "10" for n in range(1, EMPTY_RANGE + 1)]
            + [f"{z}{n:03d}" for z in near for n in range(max(x for x, _ in by[z]) + 1, max(x for x, _ in by[z]) + 1 + GAP)]
            + [f"{z}{n:03d}" for z in empty if z >= "10" for n in range(1, EMPTY_RANGE + 1)]
            + [f"{z}{n:03d}" for z in far for n in range(max(x for x, _ in by[z]) + 1, max(x for x, _ in by[z]) + 1 + GAP)])
    jobs = [c for c in jobs if c not in stops]
    print(f"probing up to {len(jobs)} codes ({len(near)} near districts first)", flush=True)

    lock = threading.Lock()
    left = set(targets)
    done = threading.Event()
    stats = {"probed": 0, "found": 0}

    def probe(code):
        if done.is_set():
            return
        d = info(code)
        time.sleep(PAUSE)
        with lock:
            stats["probed"] += 1
            if stats["probed"] % 250 == 0:
                print(f"  {stats['probed']} probed, {stats['found']} new stops, {len(left)} route stops left", flush=True)
            if not (d and d.get("Latitude")):
                return
            p = (d["Latitude"], d["Longitude"])
            stops[code] = {"code": code, "name": d["Address"], "lat": p[0], "lon": p[1], "urban": d["IsUrban"]}
            stats["found"] += 1
            hit = {t for t in left if dist(t, p) <= MATCH_M}
            if hit:
                left.difference_update(hit)
                print(f"  {code} {d['Address']} -> matches {len(hit)} route stop(s), {len(left)} left", flush=True)
                (DATA / "stops.json").write_text(json.dumps(stops, indent=0, ensure_ascii=False))
            if not left:
                done.set()

    with ThreadPoolExecutor(THREADS) as ex:
        list(ex.map(probe, jobs))
    (DATA / "stops.json").write_text(json.dumps(stops, indent=0, ensure_ascii=False))
    print(f"done: {stats['probed']} probed, {stats['found']} new stops, {len(left)} route stops still without code", flush=True)
    for t in sorted(left):
        print("  unmatched", points[t], t)


if __name__ == "__main__":
    sys.exit(main())
