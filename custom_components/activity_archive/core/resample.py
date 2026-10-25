"""Cleaning and resampling of raw channels onto a fixed time grid.

Rules: nothing is interpolated or filled in. A grid window without a measurement produces no
point. A value is the mean of the measurements in its window (distance: the last one), because
devices with smart recording sample densely while moving and a mean of raw points would be
biased. Grid windows are aligned to the Unix epoch, so two people and all channels share the
same grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from ..const import DEFAULT_PROFILE, PROFILE_COARSE, PROFILE_PRECISE, PROFILE_STANDARD
from .model import CurveSeries, RawActivity, Value

GAP_FACTOR: Final = 3  # a gap of more than 3 grid steps ends the line


@dataclass(frozen=True, slots=True)
class CurveSpec:
    """How one raw channel becomes a curve."""

    key: str
    channel: str
    unit: str
    how: str  # "mean" or "last"
    decimals: int
    lo: float  # plausible range: values outside are discarded, never replaced
    hi: float


CURVE_SPECS: Final[tuple[CurveSpec, ...]] = (
    CurveSpec("heart_rate", "heart_rate", "bpm", "mean", 0, 25, 250),
    CurveSpec("power", "power", "W", "mean", 0, 0, 3000),
    CurveSpec("altitude", "altitude", "m", "mean", 1, -500, 9000),
    CurveSpec("speed", "speed", "m/s", "mean", 2, 0, 60),
    CurveSpec("distance", "distance", "m", "last", 1, 0, 3e6),
    CurveSpec("cadence", "cadence", "rpm", "mean", 0, 0, 250),
    CurveSpec("temperature", "temperature", "°C", "mean", 1, -60, 80),
)
SPEC_BY_KEY: Final = {spec.key: spec for spec in CURVE_SPECS}


@dataclass(frozen=True, slots=True)
class Profile:
    """Thinning profile: grid step per curve, GPS tolerance and longest GPS gap."""

    name: str
    steps: dict[str, int]
    gps_epsilon_m: float
    gps_max_gap_s: int

    def gps_time_epsilon_m(self, cycling: bool) -> float | None:
        """Time-synchronous limit: twice the line tolerance, for cycling only."""
        return 2 * self.gps_epsilon_m if cycling else None


def _steps(fast: int, slow: int) -> dict[str, int]:
    return {
        "heart_rate": fast,
        "power": fast,
        "altitude": slow,
        "speed": slow,
        "distance": slow,
        "cadence": slow,
        "temperature": slow,
    }


PROFILES: Final[dict[str, Profile]] = {
    PROFILE_PRECISE: Profile(PROFILE_PRECISE, _steps(5, 10), 5.0, 30),
    PROFILE_STANDARD: Profile(PROFILE_STANDARD, _steps(15, 15), 10.0, 30),
    PROFILE_COARSE: Profile(PROFILE_COARSE, _steps(30, 30), 20.0, 60),
}


def get_profile(name: str = DEFAULT_PROFILE) -> Profile:
    """Return the named profile; unknown names raise ``KeyError``."""
    return PROFILES[name]


@dataclass(slots=True)
class CleanedSamples:
    """Samples after pause removal, merging of equal time stamps and plausibility checks."""

    t: list[float]
    channels: dict[str, list[Value]]
    in_pause: int = 0
    duplicates: int = 0
    discarded: dict[str, int] = field(default_factory=dict)


def clean(raw: RawActivity) -> CleanedSamples:
    """Drop samples inside timer pauses, merge equal time stamps, discard implausible values."""
    pauses = raw.pauses
    names = list(raw.channels)
    t_out: list[float] = []
    cols: dict[str, list[Value]] = {name: [] for name in names}
    in_pause = duplicates = 0
    for i, moment in enumerate(raw.t):
        if any(a <= moment < b for a, b in pauses):
            in_pause += 1
            continue
        if t_out and moment == t_out[-1]:
            duplicates += 1
            for name in names:  # the later sample wins, per channel and only if it has a value
                if raw.channels[name][i] is not None:
                    cols[name][-1] = raw.channels[name][i]
            continue
        t_out.append(moment)
        for name in names:
            cols[name].append(raw.channels[name][i])
    discarded: dict[str, int] = {}
    for spec in CURVE_SPECS:
        col = cols.get(spec.channel)
        if col is None:
            continue
        count = 0
        for i, value in enumerate(col):
            if value is not None and not (spec.lo <= value <= spec.hi):
                col[i] = None
                count += 1
        if count:
            discarded[spec.channel] = count
    return CleanedSamples(t_out, cols, in_pause, duplicates, discarded)


def resample(
    t: list[float], values: list[Value], step: int, how: str
) -> list[tuple[int, float, float]]:
    """Aggregate onto the epoch-aligned grid.

    Returns ``(window start, value, time of the last measurement in the window)`` for every
    window that has at least one measurement.
    """
    out: list[tuple[int, float, float]] = []
    current: int | None = None
    total = 0.0
    count = 0
    last_value = last_t = 0.0
    for moment, value in zip(t, values, strict=True):
        if value is None:
            continue
        window = int(moment // step) * step
        if window != current:
            if current is not None:
                out.append((current, total / count if how == "mean" else last_value, last_t))
            current, total, count = window, 0.0, 0
        total += value
        count += 1
        last_value, last_t = value, moment
    if current is not None:
        out.append((current, total / count if how == "mean" else last_value, last_t))
    return out


def build_curve(
    spec: CurveSpec, cleaned: CleanedSamples, step: int, not_before: int | None = None
) -> CurveSeries | None:
    """Build the curve for ``spec`` or return ``None`` if the source has no such measurement.

    ``not_before`` keeps the first point inside the activity: a window that starts before the
    activity start is stamped with the start instead.
    """
    column = cleaned.channels.get(spec.channel)
    if column is None or all(v is None for v in column):
        return None
    windows = resample(cleaned.t, column, step, spec.how)
    points: dict[int, float] = {}
    markers: list[int] = []
    for k, (window, value, last_t) in enumerate(windows):
        stamp = max(window, not_before) if not_before is not None else window
        points[stamp] = round(value, spec.decimals) if spec.decimals else float(round(value))
        is_last = k == len(windows) - 1
        if is_last or windows[k + 1][0] - window > GAP_FACTOR * step:
            markers.append(int(last_t) + 1)
    return CurveSeries(
        key=spec.key,
        unit=spec.unit,
        step_s=step,
        points=tuple(sorted(points.items())),
        markers=tuple(markers),
        decimals=spec.decimals,
    )
