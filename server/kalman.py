"""Timetable-driven tracking: the timetable is the motion model, GPS the correction.

Every stop that lists a run also gives `Time`, that run's scheduled time at that stop.
Collected per trip, they make a curve T(s): when the timetable has the bus at s metres
along its route. A bus is then described by one number that changes slowly: its delay
d = t - T(s), filtered by a small Kalman filter (state: delay and its rate of change).
Its position at any time is where the timetable puts it, shifted by the delay:
s(t) = S(t - d). Between GPS fixes, or when GPS stops, it keeps moving at the
timetable's pace with an uncertainty that grows, instead of a speed fitted from
whichever fixes happened to come in.

Measurements: a GPS fix at s (delay = t - T(s)), and a passage, when a stop that listed
the trip stops listing it (it went by between the two polls). Passages need no GPS, so
they also locate runs the feed lists without any position ("ghosts")."""
import bisect
import math

SCHED_SIGMA_S = 20.0   # s, error of one timetable time (whole minutes)
GPS_SIGMA_M = 15.0     # m, GPS + projection onto the shape
Q_D = 2.0              # s^2/s, random walk of the delay (lights, stands, traffic)
Q_R = 1e-5             # 1/s, random walk of the delay's rate
TAU_R = 600.0          # s, a delay trend fades: timetables have slack to catch up
D0_SIGMA = 120.0       # s, delay prior of a trip with no measurement yet
R0_SIGMA = 0.05        # prior of the rate (s of delay per s)
GATE = 3.0             # innovations beyond this many sigmas weigh less (a stand, a jam)
DEFAULT_PACE = 5.0     # m/s, timetable pace where only one scheduled time is known
RELAX_S = 60.0         # s, the last fix's own delay fades into the filtered one this fast
RELAX_MAX = 0.75 * RELAX_S  # cap on that offset, so the shown bus never moves backwards


class Timetable:
    """A trip's scheduled times at the stops that listed it: stop index -> unix time."""

    def __init__(self):
        self.at = {}
        self.version = 0
        self._curve = None  # (version, ref version, points)

    def add(self, i, ts):
        if self.at.get(i) != ts:
            self.at[i] = ts
            self.version += 1

    def first(self):
        return min(self.at) if self.at else None

    def curve(self, stops, ref=None):
        """[(s, T)] strictly increasing in both, from this trip's times and, past the
        ends of what this trip was listed with, another trip of the same route shifted
        to match (the timetable's shape is the same, only the start time differs).
        Several stops in the same minute become one point at their middle."""
        key = (self.version, ref and ref.version, id(ref))
        if self._curve and self._curve[0] == key:
            return self._curve[1]
        at = dict(self.at)
        if ref is not None and at:
            common = [i for i in at if i in ref.at]
            if common:
                lo, hi = min(at), max(at)
                k_hi, k_lo = max(common), min(common)
                for i, ts in ref.at.items():
                    if i > hi:
                        at[i] = ts + (at[k_hi] - ref.at[k_hi])
                    elif i < lo:
                        at[i] = ts + (at[k_lo] - ref.at[k_lo])
        pts, last_t = [], None
        for i in sorted(at):
            if i >= len(stops):
                continue
            ts = at[i]
            if last_t is not None and ts < last_t:
                continue  # out of order: a loop matched to the wrong pass, or a bad answer
            last_t = ts
            pts.append((stops[i]["s"], ts))
        out = []
        j = 0
        while j < len(pts):
            k = j
            while k + 1 < len(pts) and pts[k + 1][1] == pts[j][1]:
                k += 1
            s = sum(p[0] for p in pts[j:k + 1]) / (k - j + 1)
            if not out or s > out[-1][0] + 1:
                out.append((s, pts[j][1]))
            j = k + 1
        self._curve = (key, out)
        return out


class Schedule:
    """T(s) and its inverse S(T) for one curve, extrapolated past its ends at the
    pace of the nearest stretch."""

    def __init__(self, pts, length):
        self.pts = pts
        self.length = length
        self.ss = [p[0] for p in pts]
        self.ts = [p[1] for p in pts]

    def __bool__(self):
        return bool(self.pts)

    def _pace(self, a, b):
        (s1, t1), (s2, t2) = self.pts[a], self.pts[b]
        return (s2 - s1) / (t2 - t1) if t2 > t1 else DEFAULT_PACE

    def pace_at(self, s):
        """Scheduled m/s around s (stands at stops included)."""
        if len(self.pts) < 2:
            return DEFAULT_PACE
        k = min(max(bisect.bisect_right(self.ss, s), 1), len(self.pts) - 1)
        return self._pace(k - 1, k)

    def t_at(self, s):
        if len(self.pts) == 1:
            return self.ts[0] + (s - self.ss[0]) / DEFAULT_PACE
        k = min(max(bisect.bisect_right(self.ss, s), 1), len(self.pts) - 1)
        s1, t1 = self.pts[k - 1]
        return t1 + (s - s1) / self._pace(k - 1, k)

    def s_at(self, t):
        if len(self.pts) == 1:
            s = self.ss[0] + (t - self.ts[0]) * DEFAULT_PACE
        else:
            k = min(max(bisect.bisect_right(self.ts, t), 1), len(self.pts) - 1)
            s1, t1 = self.pts[k - 1]
            s = s1 + (t - t1) * self._pace(k - 1, k)
        return min(max(s, 0.0), self.length)


class DelayFilter:
    """Kalman filter on x = [delay (s), delay rate (s/s)]. The rate decays with TAU_R."""

    def __init__(self, t, d0=0.0, sd=D0_SIGMA):
        self.t = t
        self.x = [d0, 0.0]
        self.P = [[sd * sd, 0.0], [0.0, R0_SIGMA ** 2]]
        self.n = 0
        self.last = None  # (t, measured delay, innovation, sigma of the measurement)

    @staticmethod
    def _step(x, P, dt):
        if dt <= 0:
            return list(x), [row[:] for row in P]
        a = math.exp(-dt / TAU_R)
        b = TAU_R * (1 - a)
        x2 = [x[0] + b * x[1], a * x[1]]
        p00 = P[0][0] + 2 * b * P[0][1] + b * b * P[1][1] + Q_D * dt
        p01 = a * (P[0][1] + b * P[1][1])
        p11 = a * a * P[1][1] + Q_R * dt
        return x2, [[p00, p01], [p01, p11]]

    def at(self, t):
        """(delay, sigma) expected at t, without changing the filter."""
        x, P = self._step(self.x, self.P, t - self.t)
        return x[0], math.sqrt(max(P[0][0], 0.0))

    def delay_at(self, t):
        return self._step(self.x, self.P, t - self.t)[0][0]

    def update(self, t, z, sigma):
        """Take a measured delay z (s) at t with standard deviation sigma. A measurement
        older than the filter's time is taken at the filter's time."""
        x, P = self._step(self.x, self.P, t - self.t)
        self.t = max(self.t, t)
        r = sigma * sigma
        s = P[0][0] + r
        y = z - x[0]
        if self.n >= 2 and abs(y) > GATE * math.sqrt(s):
            r *= (abs(y) / (GATE * math.sqrt(s))) ** 2  # robust: a big jump moves it, slower
            s = P[0][0] + r
        k0, k1 = P[0][0] / s, P[0][1] / s
        self.x = [x[0] + k0 * y, x[1] + k1 * y]
        self.P = [[(1 - k0) * P[0][0], (1 - k0) * P[0][1]],
                  [(1 - k0) * P[0][1], P[1][1] - k1 * P[0][1]]]
        self.n += 1
        self.last = (t, z, y, sigma)
        return y


class TimetableTrack:
    """A trip's timetable and delay filter, shared by GPS-tracked buses and ghosts."""

    def __init__(self, variant):
        self.variant = variant
        self.tt = Timetable()
        self.kf = None
        self.anchor = None   # (t, delay) of the last GPS fix: shown position starts there
        self.passages = 0
        self.passed_i = -1   # furthest stop it was timed going by
        self.ref = None      # another trip's Timetable on this route, to extend ours

    def schedule(self):
        return Schedule(self.tt.curve(self.variant.stops, self.ref), self.variant.shape.length)

    def ready(self):
        return bool(self.tt.at) and self.kf is not None

    def _filter(self, t):
        if self.kf is None:
            self.kf = DelayFilter(t)
        return self.kf

    def measure_s(self, t, s):
        """A GPS fix at s metres. Early at the first stop means waiting to depart: no news."""
        sch = self.schedule()
        if not sch:
            return None
        z = t - sch.t_at(s)
        if z < 0 and s < self.variant.stops[min(1, len(self.variant.stops) - 1)]["s"] - 20:
            return None
        sigma = math.hypot(SCHED_SIGMA_S, GPS_SIGMA_M / max(sch.pace_at(s), 1.0))
        self._filter(t).update(t, z, sigma)
        self.anchor = (t, z)
        return z

    def measure_passage(self, i, t0, t1):
        """It went by stop i between t0 (still listed) and t1 (not any more)."""
        sch = self.schedule()
        if not sch:
            return None
        tm = (t0 + t1) / 2
        z = tm - sch.t_at(self.variant.stops[i]["s"])
        sigma = math.hypot(SCHED_SIGMA_S, (t1 - t0) / math.sqrt(12))
        self._filter(tm).update(tm, z, sigma)
        self.passages += 1
        self.passed_i = max(self.passed_i, i)
        return z

    def not_past(self, i, t):
        """Still listed at stop i at t: it hasn't got there yet, so its delay is at least
        what reaching it now would mean (applied to ghosts and to buses whose GPS froze or
        went, see the tracker). Not for a stop it was already timed passing: a stop behind
        can keep listing it a while."""
        if self.kf is None or i <= self.passed_i:
            return
        sch = self.schedule()
        if not sch:
            return
        lo = t - sch.t_at(self.variant.stops[i]["s"])
        d, _ = self.kf.at(t)
        if d < lo:
            self.kf.x[0] += lo - d

    def delay(self, t):
        """(delay to show at t, sigma). From the last fix the bus starts at its own measured
        delay and fades into the filter's, so it starts where the GPS put it."""
        d, sd = self.kf.at(t)
        if self.anchor:
            ta, za = self.anchor
            e = min(max(za - self.kf.delay_at(ta), -RELAX_MAX), RELAX_MAX)
            d += e * math.exp(-max(0.0, t - ta) / RELAX_S)
        return d, sd

    def position(self, t):
        """(s, sigma in metres, scheduled pace)."""
        sch = self.schedule()
        d, sd = self.delay(t)
        s = sch.s_at(t - d)
        pace = sch.pace_at(s)
        return s, sd * pace, pace

    def eta(self, s_to, now):
        """Seconds until it reaches s_to: its scheduled time there plus the delay expected
        by then (the rate fades, so solve a couple of times)."""
        sch = self.schedule()
        if not sch or self.kf is None:
            return None
        ts = sch.t_at(s_to)
        a = now
        for _ in range(3):
            a = max(now, ts + self.delay(a)[0])
        return a - now
