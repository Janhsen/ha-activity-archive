"""From one raw activity to the states that will be written.

Rules that matter here: the activity sensor carries only the device's own totals (never numbers
derived from the thinned curves), a channel the source lacks produces no curve, and every state
is stamped with its real measurement time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Final

from ..const import (
    DEFAULT_PROFILE,
    MARKER_STATE,
    MAX_ATTRIBUTES_BYTES,
    MAX_STATE_LENGTH,
    MIN_DURATION_S,
    SCHEMA_VERSION,
    TRACKER_STATE,
)
from . import resample, simplify, sports
from .model import CurveSeries, GpsTrack, RawActivity, StatePoint

# Entity id suffixes (after ``activity_archive_<person>``) per curve key; the position is a tracker.
# All ids start with ``activity_archive_`` so that recorder ``entity_globs`` cover them.
ENTITY_SUFFIX: Final[dict[str, str]] = {
    "heart_rate": "heart_rate",
    "altitude": "altitude",
    "speed": "speed",
    "distance": "distance",
    "cadence": "cadence",
    "power": "power",
    "temperature": "temperature",
}
POSITION_KEY: Final = "position"

# Attributes dropped first when the 12 KB budget is exceeded (least important first).
_DROP_ORDER: Final = ("curves", "sport_raw", "start_local", "calories_kcal", "max_cadence")


class SkippedError(Exception):
    """The activity is not imported; the message goes into the report."""


def activity_entity(person: str) -> str:
    """Entity id of the activity sensor."""
    return f"sensor.activity_archive_{person}"


def curve_entity(person: str, key: str) -> str:
    """Entity id of a scalar curve sensor."""
    return f"sensor.activity_archive_{person}_{ENTITY_SUFFIX[key]}"


def position_entity(person: str) -> str:
    """Entity id of the position tracker."""
    return f"device_tracker.activity_archive_{person}_position"


def entity_for(person: str, key: str) -> str:
    """Entity id for a curve key including ``position``."""
    return position_entity(person) if key == POSITION_KEY else curve_entity(person, key)


@dataclass(slots=True)
class ActivityPlan:
    """Everything needed to write one activity."""

    person: str
    activity_id: str
    start: int
    end: int
    title: str
    sport: sports.SportInfo
    attributes: dict[str, Any]
    curves: list[CurveSeries] = field(default_factory=list)
    gps: GpsTrack | None = None
    warnings: list[str] = field(default_factory=list)

    def entity_states(self) -> dict[str, list[StatePoint]]:
        """States per entity, each list ascending by time. The activity sensor comes last."""
        out: dict[str, list[StatePoint]] = {}
        marker_attrs = (("activity_id", self.activity_id),)
        for curve in self.curves:
            attrs = (("unit_of_measurement", curve.unit), ("activity_id", self.activity_id))
            states = [StatePoint(ts, _format(v, curve.decimals), attrs) for ts, v in curve.points]
            states += [StatePoint(ts, MARKER_STATE, attrs) for ts in curve.markers]
            out[curve_entity(self.person, curve.key)] = sorted(states, key=lambda s: s.ts)
        if self.gps and self.gps.points:
            states = [
                StatePoint(
                    ts,
                    TRACKER_STATE,
                    (
                        ("latitude", round(lat, 6)),
                        ("longitude", round(lon, 6)),
                        ("source_type", "gps"),
                        ("activity_id", self.activity_id),
                    ),
                )
                for ts, lat, lon in self.gps.points
            ]
            states += [StatePoint(ts, MARKER_STATE, marker_attrs) for ts in self.gps.markers]
            out[position_entity(self.person)] = sorted(states, key=lambda s: s.ts)
        out[activity_entity(self.person)] = [
            StatePoint(self.start, self.title[:MAX_STATE_LENGTH], tuple(self.attributes.items()))
        ]
        return out

    def counts(self) -> dict[str, int]:
        """Return the number of states per entity."""
        return {entity: len(states) for entity, states in self.entity_states().items()}


def _format(value: float, decimals: int) -> str:
    return str(int(value)) if decimals == 0 else str(round(value, decimals))


def available_curves(raw: RawActivity) -> frozenset[str]:
    """Curve keys the raw data can provide (used for the source rule)."""
    keys = {spec.key for spec in resample.CURVE_SPECS if raw.has(spec.channel)}
    if raw.has("lat") and raw.has("lon"):
        keys.add(POSITION_KEY)
    return frozenset(keys)


def sport_of(raw: RawActivity) -> sports.SportInfo:
    """Normalised sport of a raw activity."""
    if raw.source_format == "fit":
        return sports.from_fit(raw.sport_raw, raw.sub_sport_raw)
    if raw.source_format == "tcx" and raw.sport_raw:
        mapping = {"Biking": "Ride", "Running": "Run"}
        return sports.from_strava(mapping.get(raw.sport_raw, raw.sport_raw))
    return sports.from_gpx_type(raw.sport_raw)


def fit_attributes(attributes: dict[str, Any], limit: int = MAX_ATTRIBUTES_BYTES) -> dict[str, Any]:
    """Return attributes of at most ``limit`` JSON bytes; optional keys are dropped first."""
    out = dict(attributes)

    def size() -> int:
        return len(json.dumps(out, ensure_ascii=False, default=str).encode())

    for key in _DROP_ORDER:
        if size() <= limit:
            break
        out.pop(key, None)
    while size() > limit and out:  # last resort: shorten long text values, then drop the largest
        longest = max(out, key=lambda k: len(json.dumps(out[k], default=str)))
        if isinstance(out[longest], str) and len(out[longest]) > 64:
            out[longest] = out[longest][:64]
        else:
            out.pop(longest)
    return out


def build_plan(
    raw: RawActivity,
    *,
    person: str,
    source: str,
    source_id: str | None = None,
    profile: str = DEFAULT_PROFILE,
    title: str | None = None,
    sport: sports.SportInfo | None = None,
    min_duration_s: float = MIN_DURATION_S,
    assumed_tz: tzinfo | None = None,
    include_start_position: bool = False,
) -> ActivityPlan:
    """Build the plan for one activity; raises :class:`SkippedError` if it should not be written."""
    prof = resample.get_profile(profile)
    cleaned = resample.clean(raw)
    if not cleaned.t:
        raise SkippedError("no samples left after removing pauses")
    sport = sport or sport_of(raw)
    first, last = cleaned.t[0], cleaned.t[-1]
    start = int(min(raw.start, first)) if raw.start is not None else int(first)
    elapsed = raw.session.get("elapsed_s")
    duration = float(elapsed) if isinstance(elapsed, int | float) else last - first
    if duration < min_duration_s:
        raise SkippedError(f"shorter than {int(min_duration_s)} s")
    end = start + round(duration)

    step_cadence = sport.cadence_unit == "spm"
    if step_cadence:  # FIT stores running cadence per leg; the archive keeps steps per minute
        cleaned.channels["cadence"] = [
            None if v is None else v * 2 for v in cleaned.channels["cadence"]
        ]
    curves: list[CurveSeries] = []
    for spec in resample.CURVE_SPECS:
        curve = resample.build_curve(spec, cleaned, prof.steps[spec.key], not_before=start)
        if curve is not None:
            if spec.key == "cadence" and step_cadence:
                curve = CurveSeries(
                    curve.key, "spm", curve.step_s, curve.points, curve.markers, curve.decimals
                )
            curves.append(curve)
    gps: GpsTrack | None = None
    if cleaned.channels["lat"] and any(v is not None for v in cleaned.channels["lat"]):
        gps = simplify.simplify_gps(
            cleaned.t,
            cleaned.channels["lat"],
            cleaned.channels["lon"],
            epsilon_m=prof.gps_epsilon_m,
            max_gap_s=prof.gps_max_gap_s,
            time_epsilon_m=prof.gps_time_epsilon_m(sport.uses_time_position),
        )
        if not gps.points:
            gps = None

    warnings = list(raw.warnings)
    if cleaned.discarded:
        warnings.append(f"implausible values discarded: {cleaned.discarded}")
    if gps and gps.spikes_removed:
        warnings.append(f"{gps.spikes_removed} GPS spikes removed")

    activity_id = str(start)
    attrs = _attributes(
        raw,
        sport,
        person,
        source,
        source_id,
        activity_id,
        start,
        end,
        duration,
        curves,
        gps,
        prof.name,
        assumed_tz,
        step_cadence,
        include_start_position,
    )
    text = title or raw.title or f"{sport.sport} {datetime.fromtimestamp(start, UTC):%Y-%m-%d}"
    return ActivityPlan(
        person=person,
        activity_id=activity_id,
        start=start,
        end=end,
        title=text,
        sport=sport,
        attributes=fit_attributes(attrs),
        curves=curves,
        gps=gps,
        warnings=warnings,
    )


def _attributes(
    raw: RawActivity,
    sport: sports.SportInfo,
    person: str,
    source: str,
    source_id: str | None,
    activity_id: str,
    start: int,
    end: int,
    duration: float,
    curves: list[CurveSeries],
    gps: GpsTrack | None,
    profile: str,
    assumed_tz: tzinfo | None,
    step_cadence: bool,
    include_start_position: bool,
) -> dict[str, Any]:
    s = raw.session
    attrs: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "activity_id": activity_id,
        "person": person,
        "sources": [source],
        "sport": sport.sport,
        "sport_raw": sport.raw,
        "start_time": datetime.fromtimestamp(start, UTC).isoformat(),
        "end_time": datetime.fromtimestamp(end, UTC).isoformat(),
        "duration_s": round(duration),
        "has_gps": gps is not None,
        "has_hr": any(c.key == "heart_rate" for c in curves),
        "trainer": sport.trainer,
        "profile": profile,
        "curves": {c.key: {"n": len(c.points), "step_s": c.step_s} for c in curves}
        | ({POSITION_KEY: {"n": len(gps.points), "eps_m": gps.epsilon_m}} if gps else {}),
    }
    if source_id:
        attrs[f"{source.split('_')[0]}_id"] = source_id
    if sport.sport_type:
        attrs["sport_type"] = sport.sport_type
    offset, tz_source = raw.tz_offset_s, raw.tz_source
    if offset is None and assumed_tz is not None:
        delta = datetime.fromtimestamp(start, assumed_tz).utcoffset()
        offset = int(delta.total_seconds()) if delta is not None else None
        tz_source = "assumed"
    if offset is not None:
        local = datetime.fromtimestamp(start, UTC) + timedelta(seconds=offset)
        attrs |= {
            "utc_offset_s": offset,
            "start_local": local.replace(tzinfo=None).isoformat(),
            "local_date": local.date().isoformat(),
        }
    attrs["tz_source"] = tz_source if offset is not None else "unknown"
    mapping = {  # session key -> attribute (the device's own totals only)
        "timer_s": "moving_time_s",
        "distance_m": "distance_m",
        "ascent_m": "elevation_gain_m",
        "descent_m": "elevation_loss_m",
        "avg_speed_mps": "avg_speed_mps",
        "max_speed_mps": "max_speed_mps",
        "avg_hr": "avg_hr_bpm",
        "max_hr": "max_hr_bpm",
        "avg_power": "avg_power_w",
        "max_power": "max_power_w",
        "avg_cadence": "avg_cadence",
        "max_cadence": "max_cadence",
        "avg_temp": "avg_temp_c",
        "calories": "calories_kcal",
    }
    for key, name in mapping.items():
        if key in s:
            value = s[key]
            if step_cadence and key in ("avg_cadence", "max_cadence"):
                value = value * 2  # type: ignore[operator]
            attrs[name] = round(value, 2) if isinstance(value, float) else value
    if include_start_position and gps and gps.points:
        attrs["start_lat"], attrs["start_lon"] = (
            round(gps.points[0][1], 6),
            round(gps.points[0][2], 6),
        )
    return attrs
