"""GPS simplification: spike filter, Douglas-Peucker and its time-synchronous variant (TD-TR).

Only real fixes survive: points are dropped, never moved, averaged or interpolated. Positions are
projected to a local plane (equirectangular around the run's mean latitude), good to centimetres
over the distances of one activity and far inside the 5 m budget.

Guarantee: for every kept segment ``a..b`` of a run, every dropped raw point in between lies within
``epsilon_m`` of the segment, within ``time_epsilon_m`` of the position the segment has at that
point's time (when a time limit is set), and ``t[b] - t[a] <= max_gap_s`` unless ``b`` directly
follows ``a`` in the raw data.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence

from .model import GpsTrack, Value

EARTH_RADIUS_M = 6_371_008.8
MAX_SPEED_MPS = 50.0  # faster jumps between two fixes are GPS spikes
MAX_CONSECUTIVE_SPIKES = 10  # after this many rejected fixes the new place is accepted


def project(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Project degrees to metres on a plane tangent at latitude ``lat0``."""
    k = math.pi / 180 * EARTH_RADIUS_M
    return lon * k * math.cos(math.radians(lat0)), lat * k


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - ax - u * dx, py - ay - u * dy)


def remove_spikes(
    t: Sequence[float],
    lat: Sequence[Value],
    lon: Sequence[Value],
    *,
    max_speed: float = MAX_SPEED_MPS,
    max_consecutive: int = MAX_CONSECUTIVE_SPIKES,
) -> tuple[list[Value], list[Value], int]:
    """Blank fixes that would need more than ``max_speed`` from the last accepted fix.

    Returns new ``lat``/``lon`` lists (rejected fixes become ``None``) and the number rejected.
    """
    out_lat, out_lon = list(lat), list(lon)
    last: int | None = None
    bad_run = 0
    rejected = 0
    for i in range(len(t)):
        if lat[i] is None or lon[i] is None:
            continue
        if last is None:
            last = i
            continue
        ref = lat[last]
        ax, ay = project(lat[last], lon[last], ref)  # type: ignore[arg-type]
        bx, by = project(lat[i], lon[i], ref)  # type: ignore[arg-type]
        dt = max(t[i] - t[last], 1.0)
        if math.hypot(bx - ax, by - ay) / dt > max_speed and bad_run < max_consecutive:
            out_lat[i] = out_lon[i] = None
            bad_run += 1
            rejected += 1
        else:
            last = i
            bad_run = 0
    return out_lat, out_lon, rejected


def split_runs(indexes: Sequence[int], t: Sequence[float], max_gap_s: float) -> list[list[int]]:
    """Group sample indexes into runs; a time gap above ``max_gap_s`` starts a new run."""
    runs: list[list[int]] = []
    for i in indexes:
        if runs and t[i] - t[runs[-1][-1]] <= max_gap_s:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def simplify_run(
    x: Sequence[float],
    y: Sequence[float],
    t: Sequence[float],
    epsilon_m: float,
    max_gap_s: float,
    time_epsilon_m: float | None = None,
) -> list[int]:
    """Return the indexes to keep of one continuous run (see the module guarantee)."""
    n = len(x)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    weight = epsilon_m / time_epsilon_m if time_epsilon_m else 0.0
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay, bx, by = x[a], y[a], x[b], y[b]
        span = t[b] - t[a]
        worst, worst_i = -1.0, a + 1
        for i in range(a + 1, b):
            d = _segment_distance(x[i], y[i], ax, ay, bx, by)
            if weight:
                u = (t[i] - t[a]) / span if span else 0.0
                sync = math.hypot(x[i] - (ax + u * (bx - ax)), y[i] - (ay + u * (by - ay)))
                d = max(d, sync * weight)
            if d > worst:
                worst, worst_i = d, i
        if worst > epsilon_m:
            split = worst_i
        elif span > max_gap_s:
            split = min(max(bisect_left(t, t[a] + span / 2, a + 1, b), a + 1), b - 1)
        else:
            continue
        keep[split] = True
        stack.append((a, split))
        stack.append((split, b))
    return [i for i, flag in enumerate(keep) if flag]


def simplify_gps(
    t: Sequence[float],
    lat: Sequence[Value],
    lon: Sequence[Value],
    *,
    epsilon_m: float = 5.0,
    max_gap_s: int = 30,
    time_epsilon_m: float | None = None,
    spike_filter: bool = True,
) -> GpsTrack:
    """Reduce a GPS track to the fixes that keep the line within the given tolerances.

    Gaps longer than ``max_gap_s`` split the track; each run gets an ``unavailable`` marker one
    second after its last fix so charts do not draw across the gap.
    """
    rejected = 0
    if spike_filter:
        lat, lon, rejected = remove_spikes(t, lat, lon)
    valid = [i for i in range(len(t)) if lat[i] is not None and lon[i] is not None]
    if not valid:
        return GpsTrack(
            points=(), epsilon_m=epsilon_m, max_gap_s=max_gap_s, spikes_removed=rejected
        )
    points: dict[int, tuple[float, float]] = {}
    markers: list[int] = []
    for run in split_runs(valid, t, max_gap_s):
        lat0 = sum(lat[i] for i in run) / len(run)  # type: ignore[misc]
        xs, ys = zip(*(project(lat[i], lon[i], lat0) for i in run), strict=True)  # type: ignore[arg-type]
        ts = [t[i] for i in run]
        for k in simplify_run(xs, ys, ts, epsilon_m, max_gap_s, time_epsilon_m):
            i = run[k]
            points.setdefault(int(t[i]), (lat[i], lon[i]))  # type: ignore[arg-type]
        markers.append(int(t[run[-1]]) + 1)
    ordered = tuple((ts, la, lo) for ts, (la, lo) in sorted(points.items()))
    return GpsTrack(
        points=ordered,
        markers=tuple(markers),
        epsilon_m=epsilon_m,
        time_epsilon_m=time_epsilon_m,
        max_gap_s=max_gap_s,
        raw_points=len(valid),
        spikes_removed=rejected,
    )


# ---------------------------------------------------------------- measurement helpers
def line_deviations(
    t: Sequence[float],
    lat: Sequence[Value],
    lon: Sequence[Value],
    kept: Sequence[tuple[int, float, float]],
    *,
    max_gap_s: float = 30,
) -> tuple[float, float]:
    """Return ``(max cross-track error, max time-synchronous error)`` in metres.

    Every raw fix is compared with the kept segment that spans its time. Fixes outside every
    kept segment (inside a gap) are ignored. Used by tests and by the post-import verification.
    """
    if len(kept) < 2:
        return 0.0, 0.0
    lat0 = sum(p[1] for p in kept) / len(kept)
    kt = [p[0] for p in kept]
    proj = [project(p[1], p[2], lat0) for p in kept]
    worst_cross = worst_sync = 0.0
    for i in range(len(t)):
        if lat[i] is None or lon[i] is None:
            continue
        j = bisect_left(kt, t[i])
        if j < len(kt) and kt[j] == t[i]:
            continue  # a kept fix itself
        if j == 0 or j >= len(kt) or kt[j] - kt[j - 1] > max_gap_s:
            continue
        a, b = j - 1, j
        px, py = project(lat[i], lon[i], lat0)
        worst_cross = max(worst_cross, _segment_distance(px, py, *proj[a], *proj[b]))
        span = kt[b] - kt[a]
        u = (t[i] - kt[a]) / span if span else 0.0
        qx = proj[a][0] + u * (proj[b][0] - proj[a][0])
        qy = proj[a][1] + u * (proj[b][1] - proj[a][1])
        worst_sync = max(worst_sync, math.hypot(px - qx, py - qy))
    return worst_cross, worst_sync
