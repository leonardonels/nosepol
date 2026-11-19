"""Per-trip state along the route: s(t) = s0 + v*(t - t_now), least squares
over a short window of GPS fixes projected onto the route shape."""
import math

WINDOW_S = 150      # fit window
MAX_FIXES = 8
MAX_EXTRAP_S = 60   # don't dead-reckon further than this past the last fix
OFFROUTE_M = 80     # fixes further than this from the shape are not used in the fit


class Track:
    def __init__(self, trip, variant):
        self.trip = trip
        self.variant = variant
        self.fixes = []        # (t, s, perp, lat, lon)
        self.meta = {}

    def add_fix(self, t, lat, lon):
        if self.fixes and (lat, lon) == self.fixes[-1][3:5]:
            return False  # same GPS batch seen via another stop, or bus not refreshed
        hint = self.predict(t)[0] if self.fixes else None
        perp, s = self.variant.shape.project(lat, lon, hint)
        self.fixes.append((t, s, perp, lat, lon))
        self.fixes = [f for f in self.fixes if t - f[0] <= WINDOW_S][-MAX_FIXES:]
        return True

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

    def predict(self, now):
        a, v, res, t_ref, n = self.fit()
        dt = min(max(0.0, now - t_ref), MAX_EXTRAP_S)
        s = a + (v or 0.0) * dt
        return min(max(s, 0.0), self.variant.shape.length), v, res, n

    def state(self, now):
        s, v, res, n = self.predict(now)
        lat, lon = self.variant.shape.at(s)
        nxt = self.variant.next_stop(s)
        eta = None
        if nxt and v and v > 0.5:
            eta = round((nxt["s"] - s) / v)
        last = self.fixes[-1]
        return {
            "trip": self.trip,
            "variant": self.variant.key,
            "s": round(s, 1),
            "length": round(self.variant.shape.length),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "v": None if v is None else round(v, 2),
            "residual": None if res is None else round(res, 1),
            "n_fixes": n,
            "age": round(now - last[0]),
            "gps": {"lat": last[3], "lon": last[4], "offroute": round(last[2], 1)},
            "next_stop": nxt and {"code": nxt["code"], "name": nxt["name"],
                                  "dist": round(nxt["s"] - s), "eta_s": eta},
            **self.meta,
        }
