#!/usr/bin/env python3
"""Are buses missing? Polls EVERY coded stop of the cached routes, a few rounds,
and reports the trips the server's poll set (every STOP_STRIDE-th stop + terminus)
would not have seen, with where they are along their route.

    python3 tools/check_coverage.py [--rounds 6] [--every 25]

It also checks the assumption that a stop lists only one run per line and
direction. Read-only: it doesn't touch data/.
"""
import argparse
import collections
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "server"))
from app import Tracker, get, line_name  # noqa: E402

LINES = ["T06", "T08", "T09"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--every", type=int, default=25, help="seconds between rounds (one GPS batch)")
    ap.add_argument("--lines", default=",".join(LINES))
    a = ap.parse_args()

    tr = Tracker(a.lines.split(","))
    variants = list(tr.variants.values())
    poll = set(tr.poll_set())
    full = sorted({st["code"] for v in variants for st in v.stops if st["code"]})
    nocode = sum(1 for v in variants for st in v.stops if not st["code"])
    print(f"{len(variants)} variants, {len(full)} coded stops, poll set {len(poll)}, "
          f"{nocode} route stops without a code (never polled)\n")

    pool = ThreadPoolExecutor(4)
    totals = collections.Counter()
    for rnd in range(a.rounds):
        t0 = time.time()
        answers = dict(pool.map(lambda c: (c, _runs(c)), full))
        seen_full, seen_poll, multi = {}, set(), []
        for code, runs in answers.items():
            per = collections.Counter()
            for r in runs:
                if not tr.wants(r["Line"]) or not r.get("IsStarted") or not r.get("Latitude"):
                    continue
                trip = f"{r['Line']}:{r['Race']}"
                seen_full.setdefault(trip, r)
                if code in poll:
                    seen_poll.add(trip)
                per[(r["Line"], r["Direction"])] += 1
            multi += [(code, k, n) for k, n in per.items() if n > 1]
        missed = [t for t in seen_full if t not in seen_poll]
        totals["full"] += len(seen_full)
        totals["missed"] += len(missed)
        print(f"round {rnd + 1}: {len(seen_full)} started trips on all stops, "
              f"{len(seen_poll)} on the poll set, {len(missed)} missed")
        for trip in missed:
            r = seen_full[trip]
            where = _where(tr, trip, r)
            print(f"   MISSED {line_name(r['Line'])} {r['Direction']} -> {r['Destination']}  "
                  f"trip {trip} vehicle {r['Vehicle']}  {where}")
        for code, (line, d), n in multi:
            print(f"   note: stop {code} lists {n} started runs of {line} {d} (more than one)")
        if rnd < a.rounds - 1:
            time.sleep(max(0, a.every - (time.time() - t0)))
    if totals["full"]:
        print(f"\nmissed {totals['missed']} of {totals['full']} trip sightings "
              f"({100 * totals['missed'] / totals['full']:.0f}%)")


def _runs(code):
    try:
        return get("mrcruns", StopCode=code, IsUrban="true") or []
    except Exception as e:
        print(f"   stop {code}: {e}", file=sys.stderr)
        return []


def _where(tr, trip, r):
    key = tr.race_variant.get(trip)
    cands = [tr.variants[key]] if key else [v for v in tr.variants.values()
                                            if v.line == r["Line"] and v.direction == r["Direction"]]
    if not cands:
        return f"at {r['Latitude']:.5f},{r['Longitude']:.5f} (unknown variant)"
    v = cands[0]
    perp, s = v.shape.project(r["Latitude"], r["Longitude"])
    prev = max((st for st in v.stops if st["s"] <= s), key=lambda st: st["s"], default=v.stops[0])
    return f"{s / 1000:.1f}/{v.shape.length / 1000:.1f} km, after '{prev['name']}' ({perp:.0f} m off route)"


if __name__ == "__main__":
    main()
