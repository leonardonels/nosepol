#!/usr/bin/env python3
"""Report on a long study run's logs (NOSEPOL_LOG_DIR, see server/app.py).

    python3 tools/study_report.py LOG_DIR [--line T09] [--since 2026-11-28]
        [--hours 7-9] [--days wd] [--by-hour]

--hours (local time, inclusive, may wrap: 22-5) and --days (wd, sa, su) keep only
predictions made then, so night and rush hour aren't pooled; --by-hour adds a table of
the main numbers per hour band.

From a server running with NOSEPOL_EXPORT_TOKEN set (logs and learned data, .tar.gz):

    curl -fsS -H "Authorization: Bearer $NOSEPOL_EXPORT_TOKEN" \
        https://nosepol.leonardonels.com/api/study/export -o study.tgz   # [?since=YYYY-MM-DD]
    mkdir -p study-logs && tar -xzf study.tgz -C study-logs
    python3 tools/study_report.py study-logs/logs

ETA (eta-*.jsonl, one line per scored prediction, signed errors in s, + = predicted
too late): per horizon, each predictor's bias and spread, the correlation of the
history (h) and timetable (k) errors, and what three blends would have scored on the
same predictions: the one served (b), inverse-variance weights (ignores correlation)
and covariance-aware weights w = S^-1 1 / 1' S^-1 1, fitted on the first half of the
run and scored on the second, so it can't flatter itself. Then whether the errors grow
with the filter's own sigma and with GPS age (if so, per-bus weights would pay off).

Frozen GPS (frozen-*.jsonl): how each stretch on the timetable ended, and when the
GPS came back, how far the timetable had the bus from it.

Rate study (status-*.jsonl): the last snapshot's table."""
import argparse
import glob
import json
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")  # the server's buckets are local time
BANDS = [("night", 0, 5), ("early", 6, 6), ("am peak", 7, 9), ("day", 10, 15),
         ("pm peak", 16, 19), ("evening", 20, 23)]
HORIZONS = [(180, "0-3 min"), (600, "3-10 min"), (1800, "10-30 min")]


def load(d, kind, since=None):
    rows = []
    for p in sorted(glob.glob(os.path.join(d, f"{kind}-*.jsonl"))):
        if since and os.path.basename(p)[len(kind) + 1:len(kind) + 11] < since:
            continue
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass  # a line cut by a stop mid-write
    return rows


def when(ts):
    """(local hour, day type) of a unix time, as the server buckets it."""
    lt = datetime.fromtimestamp(ts, TZ)
    return lt.hour, "wd" if lt.weekday() < 5 else ("sa" if lt.weekday() == 5 else "su")


def hour_range(text):
    lo, _, hi = text.partition("-")
    lo, hi = int(lo), int(hi or lo)
    if not (0 <= lo <= 23 and 0 <= hi <= 23):
        raise argparse.ArgumentTypeError("hours 0-23, e.g. 7-9 or 22-5")
    return set(range(lo, hi + 1)) if lo <= hi else set(range(lo, 24)) | set(range(0, hi + 1))


def keep(rows, key, hours, days):
    out = []
    for r in rows:
        h, d = when(r[key])
        if (hours is None or h in hours) and (days is None or d in days):
            out.append(r)
    return out


def by_hour(rows):
    """Per hour band and horizon: n, corr(h,k), served and covariance-aware RMSE (the
    latter fitted on the other bands' data, so a small band can't flatter itself)."""
    print("\n== By hour band (local): n  corr(h,k)  RMSE served / inverse-variance / covariance-aware")
    tagged = [(when(r["made"])[0], r) for r in rows if r.get("k") is not None]
    for lim_lo, (lim, name) in zip([15] + [l for l, _ in HORIZONS], HORIZONS):
        hz = [(hr, r) for hr, r in tagged if lim_lo < r["ahead"] <= lim]
        print(f"  {name}")
        for band, lo, hi in BANDS:
            ins = [r for hr, r in hz if lo <= hr <= hi]
            rest = [r for hr, r in hz if not lo <= hr <= hi]
            if len(ins) < 20:
                print(f"    {band:<8} n={len(ins):5d}  too few")
                continue
            h, k, b = [r["h"] for r in ins], [r["k"] for r in ins], [r["b"] for r in ins]
            sh, sk = math.sqrt(max(m2(h) - mean(h) ** 2, 0)), math.sqrt(max(m2(k) - mean(k) ** 2, 0))
            rho = ((m2(h, k) - mean(h) * mean(k)) / (sh * sk)) if sh and sk else float("nan")
            fit = rest if len(rest) >= 30 else ins
            wh, wk = [r["h"] for r in fit], [r["k"] for r in fit]
            wi, wc = iv_weight(wh, wk), cov_weight(wh, wk)
            rm = lambda e: math.sqrt(m2(e))
            print(f"    {band:<8} n={len(ins):5d}  {rho:+.2f}  {rm(b):5.0f} / "
                  f"{rm([wi * x + (1 - wi) * y for x, y in zip(h, k)]):5.0f} / "
                  f"{rm([wc * x + (1 - wc) * y for x, y in zip(h, k)]):5.0f}   (w {wi:.2f} / {wc:.2f})")


def q(xs, f):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(f * len(xs)))] if xs else None


def m2(xs, ys=None):
    ys = xs if ys is None else ys
    return sum(x * y for x, y in zip(xs, ys)) / len(xs)


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def cov_weight(h, k):
    """Weight of h minimising E[(w h + (1-w) k)^2] (raw second moments: bias counts)."""
    shh, skk, shk = m2(h), m2(k), m2(h, k)
    den = shh + skk - 2 * shk
    return 0.5 if den <= 1e-9 else (skk - shk) / den


def iv_weight(h, k):
    shh, skk = m2(h), m2(k)
    return skk / (shh + skk) if shh + skk else 0.5


def scores(errs):
    a = [abs(e) for e in errs]
    return f"{math.sqrt(m2(errs)):5.0f} {mean(a):5.0f} {q(a, 0.5):5.0f} {q(a, 0.95):5.0f}"


def eta_report(rows):
    print("== ETA: signed errors (s), + = predicted too late")
    for lim_lo, (lim, name) in zip([15] + [l for l, _ in HORIZONS], HORIZONS):
        rs = sorted((r for r in rows if lim_lo < r["ahead"] <= lim and r.get("k") is not None),
                    key=lambda r: r["made"])
        if len(rs) < 30:
            print(f"\n{name}: {len(rs)} predictions, too few")
            continue
        h, k, b = [r["h"] for r in rs], [r["k"] for r in rs], [r["b"] for r in rs]
        sh, sk = math.sqrt(m2(h) - mean(h) ** 2), math.sqrt(m2(k) - mean(k) ** 2)
        rho = ((m2(h, k) - mean(h) * mean(k)) / (sh * sk)) if sh and sk else float("nan")
        print(f"\n{name}: n={len(rs)}  corr(h,k)={rho:+.2f}")
        print(f"  bias  history {mean(h):+5.0f}  timetable {mean(k):+5.0f}  served {mean(b):+5.0f}")
        print(f"  sd    history {sh:5.0f}  timetable {sk:5.0f}")
        half = len(rs) // 2
        h1, k1, h2, k2 = h[:half], k[:half], h[half:], k[half:]
        wi, wc = iv_weight(h1, k1), cov_weight(h1, k1)
        print("  second half, fitted on the first:   RMSE  mean   p50   p95   w_hist")
        for label, errs, w in (("served (current)", b[half:], None),
                               ("history only", h2, 1.0), ("timetable only", k2, 0.0),
                               ("inverse-variance", [wi * x + (1 - wi) * y for x, y in zip(h2, k2)], wi),
                               ("covariance-aware", [wc * x + (1 - wc) * y for x, y in zip(h2, k2)], wc)):
            print(f"    {label:<18} {scores(errs)}   {'' if w is None else f'{w:.2f}'}")
        bands = (("sd", [(0, 30), (30, 60), (60, 120), (120, 1e9)]), ("age", [(0, 30), (30, 60), (60, 120), (120, 1e9)]))
        for key, cuts in bands:
            cells = []
            for lo, hi in cuts:
                sel = [r for r in rs if r.get(key) is not None and lo <= r[key] < hi]
                if len(sel) >= 10:
                    cells.append(f"{lo}-{'' if hi > 1e8 else hi}: {mean([abs(r['k']) for r in sel]):.0f}/"
                                 f"{mean([abs(r['b']) for r in sel]):.0f} (n={len(sel)})")
            if cells:
                print(f"  mean |timetable|/|served| by {'filter sigma' if key == 'sd' else 'GPS age'} (s): "
                      + "  ".join(cells))


def frozen_report(rows):
    print("\n== Frozen GPS: stretches moved on the timetable")
    if not rows:
        print("  none logged")
        return
    by = {}
    for r in rows:
        by.setdefault((r["why"], r["how"]), []).append(r)
    for (why, how), rs in sorted(by.items()):
        line = f"  {why:<9} -> {how:<9} n={len(rs):4d}  lasted median {q([r['for_s'] for r in rs], 0.5)}s"
        errs = [r["err_m"] for r in rs if r.get("err_m") is not None]
        if errs:
            a = [abs(e) for e in errs]
            line += (f"  timetable vs GPS back: bias {mean(errs):+.0f} m, |err| median {q(a, 0.5)} m,"
                     f" p95 {q(a, 0.95)} m, too far on {sum(e > 0 for e in errs)}/{len(errs)}")
        if how == "dropped":
            reasons = {}
            for r in rs:
                reasons[r.get("reason")] = reasons.get(r.get("reason"), 0) + 1
            line += f"  {reasons}"
        print(line)


def study_report(rows):
    print("\n== Rate study (last snapshot)")
    snap = next((r for r in reversed(rows) if r.get("rate_study")), None)
    if not snap:
        print("  none logged")
        return
    print(f"  uptime {snap.get('uptime_s', 0) / 3600:.1f} h, real backend {snap.get('requests_per_hour')} requests/h")
    for s in snap["rate_study"]:
        e = s.get("eta") or {}
        cells = "  ".join(f"{h} {e[h]['served']['mean']}/{e[h]['served']['median']}/{e[h]['served']['p95']}"
                          for h in ("0-3 min", "3-10 min", "10-30 min") if h in e and e[h].get("served"))
        pos = s.get("position_m") or {}
        print(f"  {s['name']:<12} {s.get('requests_per_hour')}/h  pos {pos.get('median')}/{pos.get('p95')} m"
              f"  cov {s.get('coverage')}  {cells}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log_dir")
    ap.add_argument("--line")
    ap.add_argument("--since", help="first day to read, YYYY-MM-DD")
    ap.add_argument("--hours", type=hour_range, help="local hours, inclusive: 7-9, 22-5")
    ap.add_argument("--days", type=lambda x: set(x.split(",")), help="day types: wd, sa, su (comma-separated)")
    ap.add_argument("--by-hour", action="store_true", help="also a table per hour band")
    a = ap.parse_args()
    if a.days and not a.days <= {"wd", "sa", "su"}:
        ap.error("--days takes wd, sa, su")
    eta = load(a.log_dir, "eta", a.since)
    if a.line:
        eta = [r for r in eta if r.get("line") == a.line]
    eta = keep(eta, "made", a.hours, a.days)
    if a.hours or a.days:
        print(f"(predictions made at hours {sorted(a.hours) if a.hours else 'all'}, days {sorted(a.days) if a.days else 'all'})")
    eta_report(eta)
    if a.by_hour:
        by_hour(eta)
    frozen = [r for r in load(a.log_dir, "frozen", a.since) if not a.line or r.get("line") == a.line]
    frozen_report(keep(frozen, "t", a.hours, a.days))
    study_report(load(a.log_dir, "status", a.since))


if __name__ == "__main__":
    main()
