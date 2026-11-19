"""Polyline geometry in a local metric frame (equirectangular, fine at city scale)."""
import math

R = 6371000.0


class Frame:
    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = math.radians(1) * R * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * R

    def xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def latlon(self, x, y):
        return (self.lat0 + y / self.ky, self.lon0 + x / self.kx)


class Polyline:
    """Route shape with cumulative arc length s (metres)."""

    def __init__(self, latlons, frame):
        self.frame = frame
        pts = []
        for lat, lon in latlons:
            p = frame.xy(lat, lon)
            if not pts or (p[0] - pts[-1][0]) ** 2 + (p[1] - pts[-1][1]) ** 2 > 0.01:
                pts.append(p)
        self.pts = pts
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.length = self.cum[-1]

    def project_all(self, lat, lon):
        """All per-segment projections: list of (perp_dist, s)."""
        px, py = self.frame.xy(lat, lon)
        out = []
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:])):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / L2))
            qx, qy = a[0] + t * dx, a[1] + t * dy
            out.append((math.hypot(px - qx, py - qy), self.cum[i] + t * math.sqrt(L2)))
        return out

    def project(self, lat, lon, s_hint=None):
        """Best (perp_dist, s). With s_hint, prefer matches consistent with forward
        motion, so self-overlapping routes (loops, out-and-back) don't jump."""
        best, best_cost = None, float("inf")
        for d, s in self.project_all(lat, lon):
            cost = d
            if s_hint is not None:
                ds = s - s_hint
                cost += 0.02 * abs(ds) + (0.5 * -ds if ds < -30 else 0)
            if cost < best_cost:
                best, best_cost = (d, s), cost
        return best

    def heading(self, s, back=15.0, ahead=25.0):
        """Direction of travel at s, degrees clockwise from north, taken over a stretch
        of the route (not one piece of it), so short kinks at stops don't swing it."""
        a = self.frame.xy(*self.at(max(0.0, s - back)))
        b = self.frame.xy(*self.at(min(self.length, s + ahead)))
        if math.dist(a, b) < 1:
            return None
        return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360

    def at(self, s):
        s = max(0.0, min(self.length, s))
        lo, hi = 0, len(self.cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        seg = self.cum[hi] - self.cum[lo]
        t = 0 if seg == 0 else (s - self.cum[lo]) / seg
        a, b = self.pts[lo], self.pts[hi]
        return self.frame.latlon(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
