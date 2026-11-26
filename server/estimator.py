"""Per-trip state along the route: s(t) = s0 + v*(t - t_now), least squares
over a short window of GPS fixes projected onto the route shape, and ETAs from
learned travel times between stops (History) where there are enough of them."""
import collections
import json
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from kalman import TimetableTrack

WINDOW_S = 150      # fit window
MAX_FIXES = 8
MAX_EXTRAP_S = 150  # don't dead-reckon further than this past the last fix (GPS can
                    # skip a few batches between Trieste's buildings)
OFFROUTE_M = 80     # fixes further than this from the shape are not used in the fit
STOP_AFTER_S = 90   # still listed, same position this long (~4 GPS batches) -> stopped
DWELL_S = 20        # assumed stand at every stop the bus passes while dead-reckoning
MAX_V = 13.9        # m/s, cap on the stop-corrected speed (50 km/h), never below the fitted one
ARRIVE_M = 20       # a bus is "at" a stop this far before it: arrival times are taken here
HIST_MIN_N = 3      # timed runs of a stretch before its history is used
HIST_ALPHA = 0.15   # weight of a new run once a bucket has enough
HIST_MAX_GAP_S = 240  # fixes further apart than this don't time a stretch
HIST_V = (0.8, 20.0)  # m/s, plausible average over a stretch (else: an incident, or bad GPS)
LIVE_FACTOR = (0.75, 1.5)  # how far this bus's own pace may scale the history
LOCAL_TZ = ZoneInfo("Europe/Rome")
# "kalman": position and ETA from the timetable shifted by a filtered delay (kalman.py)
# wherever the trip's timetable is known; "fit": the speed fit below only. Both are
# always computed, and /api/status scores both.
ESTIMATOR = os.environ.get("NOSEPOL_ESTIMATOR", "kalman")


class History:
    """Learned travel times between consecutive stops: from arriving at one to arriving
    at the next, so the stand at the first is included. Keyed by the two stop codes,
    so lines and variants sharing a stretch learn it together. Each stretch keeps a
    moving average per day type and hour, per hour, and overall; the narrowest one
    with HIST_MIN_N runs answers."""

    def __init__(self, path):
        self.path = path
        try:
            self.d = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            self.d = {}
        self.dirty = False

    @staticmethod
    def buckets(t):
        lt = datetime.fromtimestamp(t, LOCAL_TZ)
        day = "wd" if lt.weekday() < 5 else ("sa" if lt.weekday() == 5 else "su")
        return f"{day}{lt.hour:02d}", f"h{lt.hour:02d}", "all"

    def add(self, a, b, secs, t):
        seg = self.d.setdefault(f"{a}>{b}", {})
        for k in self.buckets(t):
            m, n = seg.get(k, (secs, 0))
            if n >= HIST_MIN_N:  # a known stretch: don't let one odd run swing it
                secs_k = min(max(secs, 0.4 * m), 3 * m)
            else:
                secs_k = secs
            n = min(n + 1, 10000)
            m += (secs_k - m) * max(1 / n, HIST_ALPHA)
            seg[k] = (round(m, 1), n)
        self.dirty = True

    def get(self, a, b, t):
        seg = self.d.get(f"{a}>{b}")
        if not seg:
            return None
        for k in self.buckets(t):
            m, n = seg.get(k, (None, 0))
            if n >= HIST_MIN_N:
                return m
        return None

    def save(self):
        if self.dirty:
            self.path.write_text(json.dumps(self.d, separators=(",", ":")))
            self.dirty = False

    def summary(self):
        n = [seg.get("all", (0, 0))[1] for seg in self.d.values()]
        return {"stretches": len(n), "usable": sum(1 for x in n if x >= HIST_MIN_N), "runs": sum(n)}


BLEND_BIN_S = 60     # weights are learned per minute of time to the stop...
BLEND_BINS = 30      # ...up to 30 min (further ahead uses the last bin)
BLEND_MIN_N = 20     # arrivals a bin needs before its own errors set its weight
BLEND_ALPHA = 0.02   # weight of a new error once a bin is warm (~50 arrivals of memory)
BLEND_ERR_CAP = 600  # s, larger errors (an incident, a detour) count as this much


class Blend:
    """Combines the history ETA and the timetable filter's into one, weighted by how far
    the stop is. Each is weighted by 1 / its mean squared error at that time to the stop
    (inverse-variance weighting), learned online from the scored arrivals: close to the
    stop the history (this bus's own recent pace) tends to win, further out the
    timetable plus delay. Saved to data/blend.json."""

    def __init__(self, path):
        self.path = path
        try:
            d = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            d = {}
        self.bins = d.get("bins") or [[0, 0.0, 0.0] for _ in range(BLEND_BINS)]  # [n, mse history, mse timetable]
        self.dirty = False

    @staticmethod
    def _bin(ahead):
        return min(max(int(ahead // BLEND_BIN_S), 0), BLEND_BINS - 1)

    def add(self, ahead, err_hist, err_tt):
        b = self.bins[self._bin(ahead)]
        b[0] = min(b[0] + 1, 100000)
        a = max(1.0 / b[0], BLEND_ALPHA)
        for j, e in ((1, err_hist), (2, err_tt)):
            e = min(abs(e), BLEND_ERR_CAP)
            b[j] += (e * e - b[j]) * a
        self.dirty = True

    def weight(self, ahead):
        """Weight of the history ETA (the timetable's is 1 - this). A bin with too few
        arrivals borrows from its neighbours; with none, half and half."""
        k = self._bin(ahead)
        n = mh = mk = 0.0
        for r in range(BLEND_BINS):
            for j in {k - r, k + r}:
                if 0 <= j < BLEND_BINS and self.bins[j][0]:
                    c, h, t = self.bins[j]
                    n, mh, mk = n + c, mh + c * h, mk + c * t
            if n >= BLEND_MIN_N:
                break
        if n < BLEND_MIN_N or mh + mk <= 0:
            return 0.5
        return mk / (mh + mk)  # = (1/mh) / (1/mh + 1/mk)

    def combine(self, eta_hist, eta_tt):
        w = self.weight((eta_hist + eta_tt) / 2)
        return w * eta_hist + (1 - w) * eta_tt, w

    def save(self):
        if self.dirty:
            self.path.write_text(json.dumps({"bins": [[n, round(h, 1), round(t, 1)] for n, h, t in self.bins]}))
            self.dirty = False

    def summary(self):
        """Per minute to the stop: arrivals learned from, and the history ETA's weight."""
        return [{"min": i, "n": b[0], "w_history": round(self.weight(i * BLEND_BIN_S + 30), 2)}
                for i, b in enumerate(self.bins) if b[0]]


class Track:
    blend = None  # the tracker's Blend, shared by every track

    def __init__(self, trip, variant, history=None):
        self.trip = trip
        self.variant = variant
        self.history = history
        self.arrivals = {}       # stop index -> time the bus reached it (interpolated)
        self.new_arrivals = []   # (stop index, time) since the tracker last took them
        self.pace = collections.deque(maxlen=4)  # (seconds taken, history's) per stretch just driven
        self.fixes = []        # (t, s, perp, lat, lon)
        self.last_report = None  # last time the API reported this trip, moved or not
        self.layover = False     # set by the tracker: trip over at the terminus, bus waiting there
        self.next_run = None     # set by the tracker: the departure it probably takes next
        self.blind = False       # set by the tracker: on a stretch no stop reports on, so dead-reckon on
        self.gps_frozen = False  # set by the tracker: listed with the same position too long mid-route
        self.gps_lost = False    # set by the tracker: still listed, but with no position
        self.hold_s = None       # set by the tracker: where its timetable ended the trip (GPS frozen or gone)
        self.tl = TimetableTrack(variant)
        self.meta = {}

    def add_fix(self, t, lat, lon):
        if self.fixes and (lat, lon) == self.fixes[-1][3:5]:
            self.last_report = t
            return False  # same GPS batch seen via another stop, or bus not moving
        if self.stopped_for(t) is not None:
            # It sat still until the last report: keep that as a fix, so the fit
            # sees "stood at s until then" instead of a slow crawl.
            self.fixes.append((self.last_report, *self.fixes[-1][1:]))
        hint = self.predict(t)[0] if self.fixes else None
        perp, s = self.variant.shape.project(lat, lon, hint)
        self.fixes.append((t, s, perp, lat, lon))
        if perp <= OFFROUTE_M:
            self.tl.measure_s(t, s)
        self.gps_frozen = self.gps_lost = False
        self.hold_s = None
        if len(self.fixes) > 1:
            self._time_stops(self.fixes[-2], self.fixes[-1])
        self.fixes = [f for f in self.fixes if t - f[0] <= WINDOW_S][-MAX_FIXES:]
        self.last_report = t
        return True

    def stand(self, t):
        """Listed again at the same position: standing there now, as far as GPS says."""
        if self.fixes[-1][2] <= OFFROUTE_M:
            self.tl.measure_s(t, self.fixes[-1][1])

    def kalman(self):
        """Use the timetable filter for position and ETA now?"""
        return ESTIMATOR == "kalman" and self.tl.ready() and not self.layover

    def estimated(self, now):
        """Position comes from the timetable, not from recent GPS."""
        return self.kalman() and (self.gps_frozen or self.gps_lost or now - self.fixes[-1][0] > STOP_AFTER_S)

    def _time_stops(self, f1, f2):
        """Arrival times at the stops passed between two fixes, interpolated, and from
        them the time taken over each stretch both ends of which it was seen reaching.
        The stretch out of the first stop isn't learned: it holds the terminus wait."""
        (t1, s1, p1), (t2, s2, p2) = f1[:3], f2[:3]
        if s2 <= s1 or t2 - t1 > HIST_MAX_GAP_S or max(p1, p2) > OFFROUTE_M:
            return
        stops = self.variant.stops
        for i, st in enumerate(stops):
            x = st["s"] - ARRIVE_M
            if not s1 < x <= s2 or i in self.arrivals:
                continue
            ta = t1 + (x - s1) / (s2 - s1) * (t2 - t1)
            self.arrivals[i] = ta
            self.new_arrivals.append((i, ta))
            j, prev = i - 1, stops[i - 1] if i else None
            if self.history is None or j < 1 or j not in self.arrivals or not (prev["code"] and st["code"]):
                continue
            secs = ta - self.arrivals[j]
            if secs <= 0 or not HIST_V[0] <= (st["s"] - prev["s"]) / secs <= HIST_V[1]:
                continue
            h = self.history.get(prev["code"], st["code"], self.arrivals[j])
            if h:
                self.pace.append((secs, h))
            self.history.add(prev["code"], st["code"], secs, self.arrivals[j])

    def live_factor(self):
        """How this bus has driven its last stretches against their history (1: as usual)."""
        if not self.pace:
            return 1.0
        f = sum(a for a, _ in self.pace) / sum(h for _, h in self.pace)
        return min(max(f, LIVE_FACTOR[0]), LIVE_FACTOR[1])

    def _hist(self, i, t):
        """History's time for the stretch from stop i to stop i+1 at time t, or None."""
        if self.history is None or i < 1:
            return None
        a, b = self.variant.stops[i]["code"], self.variant.stops[i + 1]["code"]
        return self.history.get(a, b, t) if a and b else None

    def eta(self, s_from, s_to, v, now):
        """(seconds from s_from to s_to, source). With the timetable filter running the bus
        and fresh GPS: the history ETA and the filter's combined by Blend ("blend").
        With GPS old, frozen or gone the history ETA would run on an old speed, so the
        filter's alone ("timetable"). Without a timetable: eta_hist's."""
        if self.kalman():
            e = self.tl.eta(s_to, now)
            if e is not None:
                if self.blend is not None and not self.estimated(now):
                    h = self.eta_hist(s_from, s_to, v, now)[0]
                    if h is not None:
                        return self.blend.combine(h, e)[0], "blend"
                return e, "timetable"
        return self.eta_hist(s_from, s_to, v, now)

    def eta_hist(self, s_from, s_to, v, now):
        """(seconds from s_from to s_to, "history" or "speed"). Whole stretches ahead
        take their learned time, scaled by this bus's pace on its last stretches;
        the rest of the stretch it is on, and stretches with no history, go at v
        with a stand at each stop (as time_to). The time of day for each stretch is
        when the bus should get there, so a trip into the rush hour slows down."""
        base = self.time_to(s_from, s_to, v)
        if self.history is None:
            return base, "speed"
        stops = self.variant.stops
        moving = v is not None and v > 0.5
        k = next((i for i, st in enumerate(stops) if st["s"] > s_from + 1), None)
        if k is None or stops[k]["s"] > s_to + 1:
            return base, "speed"  # on the last stretch before s_to
        f = self.live_factor()
        used = False
        left = stops[k]["s"] - s_from
        if moving:
            total = left / v
        else:  # no usable speed (one fix, or crawling): the share of the stretch left
            h = self._hist(k - 1, now) if k >= 1 else None
            if not h:
                return base, "speed"
            seg = max(stops[k]["s"] - stops[k - 1]["s"], 1.0)
            total, used = h * f * min(1.0, left / seg), True
        i = k
        while i + 1 < len(stops) and stops[i + 1]["s"] <= s_to + 1:
            h = self._hist(i, now + total)
            if h:
                total += h * f
                used = True
            elif moving:
                total += (stops[i + 1]["s"] - stops[i]["s"]) / v + DWELL_S
            else:
                return base, "speed"
            i += 1
        if s_to > stops[i]["s"] + 1:  # past the last stop (the end of the shape)
            total += DWELL_S + ((s_to - stops[i]["s"]) / v if moving else 0.0)
        return total, "history" if used else "speed"

    def stopped_for(self, now):
        """Seconds the bus has stood still, or None if it is moving (or we can't tell yet)."""
        if not self.fixes or self.last_report is None:
            return None
        since = self.fixes[-1][0]
        return now - since if self.last_report - since >= STOP_AFTER_S else None

    def fit(self):
        """Returns (s_at_last_fix_time_ref, v, residual_rms, t_ref, n)."""
        pts = [(t, s) for t, s, perp, *_ in self.fixes if perp <= OFFROUTE_M] or \
              [(t, s) for t, s, *_ in self.fixes[-1:]]
        n = len(pts)
        t_ref = pts[-1][0]
        if n == 1:
            return pts[0][1], None, None, t_ref, 1
        ts = [t - t_ref for t, _ in pts]
        ss = [s for _, s in pts]
        mt, ms = sum(ts) / n, sum(ss) / n
        stt = sum((t - mt) ** 2 for t in ts)
        v = 0.0 if stt == 0 else sum((t - mt) * (s - ms) for t, s in zip(ts, ss)) / stt
        v = max(0.0, v)  # buses don't reverse along a route
        a = ms - v * mt  # s at t_ref
        res = math.sqrt(sum((s - (a + v * t)) ** 2 for t, s in zip(ts, ss)) / n)
        return a, v, res, t_ref, n

    def cruise(self):
        """fit() with v as the speed between stops: the fitted speed is an average over
        the window, stands at the stops passed in it included. Returns like fit()."""
        a, v, res, t_ref, n = self.fit()
        if not v:
            return a, v, res, t_ref, n
        t0 = min(t for t, _, perp, *_ in self.fixes if perp <= OFFROUTE_M) \
            if any(f[2] <= OFFROUTE_M for f in self.fixes) else t_ref
        span = t_ref - t0
        if span > 0:
            s0 = a - v * span
            k = sum(1 for st in self.variant.stops[1:-1] if s0 < st["s"] < a)
            v = min(v * span / max(span - k * DWELL_S, 0.5 * span), max(MAX_V, v))
        return a, v, res, t_ref, n

    def advance(self, s, dt, v):
        """Where a bus at s is dt seconds later, driving at v between stops and standing
        DWELL_S at each one: buses stop at nearly every stop, so with no fresh GPS it
        is likelier there than past it."""
        end = self.variant.shape.length
        if not v or dt <= 0:
            return min(max(s, 0.0), end)
        for st in self.variant.stops[1:]:
            if st["s"] <= s + 1:
                continue
            need = (st["s"] - s) / v
            if need >= dt:
                break
            dt -= need
            s = st["s"]
            if dt <= DWELL_S:
                return s
            dt -= DWELL_S
        return min(max(s + v * dt, 0.0), end)

    def time_to(self, s_from, s_to, v):
        """Seconds to drive from s_from to s_to at v, standing at each stop in between."""
        if not v or v <= 0.5:
            return None
        k = sum(1 for st in self.variant.stops if s_from + 1 < st["s"] < s_to - 1)
        return max(0.0, s_to - s_from) / v + k * DWELL_S

    def predict(self, now):
        """(s, v, residual, n): v is the speed between stops (see cruise)."""
        a, v, res, t_ref, n = self.cruise()
        if self.layover or (self.stopped_for(now) is not None and not self.gps_frozen):
            return max(self.fixes[-1][1], self.hold_s or 0.0), 0.0, res, n  # hold it where it stands, no dead reckoning
        if self.kalman():
            s, _, pace = self.tl.position(now)
            if v is None or self.estimated(now):
                v = pace
            return max(s, self.fixes[-1][1]), v, res, n  # buses don't reverse
        dt = max(0.0, now - t_ref)
        if not self.blind:  # no report expected there: keep moving it (to the end at most)
            dt = min(dt, MAX_EXTRAP_S)
        return self.advance(a, dt, v), v, res, n

    def state(self, now):
        s, v, res, n = self.predict(now)
        lat, lon = self.variant.shape.at(s)
        nxt = self.variant.next_stop(s)
        stopped = None if self.gps_frozen else self.stopped_for(now)
        eta = None
        if nxt and stopped is None and (e := self.eta(s, nxt["s"], v, now)[0]) is not None:
            eta = round(e)
        last = self.fixes[-1]
        tl = None
        if self.tl.ready():
            d, sd = self.tl.delay(now)
            tl = {"delay_s": round(d), "sigma_s": round(sd), "sigma_m": round(sd * self.tl.position(now)[2]),
                  "points": len(self.tl.tt.at), "passages": self.tl.passages}
        return {
            "trip": self.trip,
            "variant": self.variant.key,
            "s": round(s, 1),
            "length": round(self.variant.shape.length),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "v": None if v is None else round(v, 2),
            "heading": None if (h := self.variant.shape.heading(s)) is None else round(h),
            "residual": None if res is None else round(res, 1),
            "n_fixes": n,
            "age": round(now - last[0]),
            "stopped_s": round(now - last[0]) if self.layover else (None if stopped is None else round(stopped)),
            "next_run": self.next_run,
            "blind": self.blind,
            "estimated": self.estimated(now),
            "gps_frozen": self.gps_frozen,
            "gps_lost": self.gps_lost,
            "timetable": tl,
            "gps": {"lat": last[3], "lon": last[4], "offroute": round(last[2], 1)},
            "next_stop": nxt and {"code": nxt["code"], "name": nxt["name"],
                                  "dist": round(nxt["s"] - s), "eta_s": eta},
            **self.meta,
        }
