"""GPX and TCX readers (standard library XML only).

A plain GPX file has position, elevation and time; heart rate, cadence, temperature and power
appear only through the usual Garmin/Strava extensions. It never has distance or speed, and this
reader does not calculate them from the positions: those channels stay empty and the archive
therefore creates no such curve for the activity.

Files with a DOCTYPE are rejected (entity expansion). A truncated file raises
:class:`IntegrityError` in ``strict`` mode, otherwise the readable part is returned.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from .model import (
    BadFileError,
    IntegrityError,
    RawActivity,
    UnsafeFileError,
    Value,
    empty_channels,
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _check_xml(data: bytes) -> None:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise UnsafeFileError("XML with DOCTYPE or ENTITY rejected")


def parse_time(text: str) -> float | None:
    """Parse an ISO 8601 time stamp to Unix seconds; a stamp without offset counts as UTC."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def _position(lat: float | None, lon: float | None) -> tuple[Value, Value]:
    if lat is None or lon is None:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None, None
    return lat, lon


_GPX_TAGS = {
    "ele": "altitude",
    "hr": "heart_rate",
    "cad": "cadence",
    "atemp": "temperature",
    "power": "power",
    "watts": "power",
    "speed": "speed",
}


def parse_gpx(data: bytes, *, strict: bool = True) -> RawActivity:
    """Decode GPX bytes into a :class:`RawActivity`."""
    _check_xml(data)
    warnings: list[str] = []
    t: list[float] = []
    ch = empty_channels()
    segment_starts: list[int] = []
    segments = 0
    name: str | None = None
    activity_type: str | None = None
    no_time = 0
    try:
        for event, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = _local(el.tag)
            if event == "start":
                if tag == "trkseg":
                    if segments and t:
                        segment_starts.append(len(t))
                    segments += 1
                continue
            if tag == "trkpt":
                no_time += _gpx_point(el, t, ch)
                el.clear()
            elif tag == "name" and name is None:
                name = (el.text or "").strip() or None
            elif tag == "type" and activity_type is None:
                activity_type = (el.text or "").strip() or None
    except ET.ParseError as err:
        if strict:
            raise IntegrityError(f"GPX XML damaged or truncated ({err})") from err
        warnings.append(f"GPX XML damaged or truncated ({err}): readable part used")
    if not t:
        raise BadFileError("GPX contains no track points with a time stamp")
    if no_time:
        warnings.append(f"{no_time} track points without a time stamp ignored")
    pauses = [(t[i - 1], t[i]) for i in segment_starts if t[i] > t[i - 1]]
    return RawActivity(
        source_format="gpx",
        t=t,
        channels=ch,
        sport_raw=activity_type,
        start=t[0],
        pauses=pauses,
        warnings=warnings,
        title=name,
    )


def _gpx_point(el: ET.Element, t: list[float], ch: dict[str, list[Value]]) -> int:
    """Append one track point; return 1 if it had to be dropped for lack of a time."""
    lat: float | None
    lon: float | None
    try:
        lat, lon = float(el.get("lat", "")), float(el.get("lon", ""))
    except ValueError:
        lat = lon = None
    values: dict[str, Value] = {}
    moment: float | None = None
    for child in el.iter():
        text = (child.text or "").strip()
        if not text:
            continue
        tag = _local(child.tag)
        if tag == "time":
            moment = parse_time(text)
        elif tag in _GPX_TAGS:
            values[_GPX_TAGS[tag]] = _number(text)
    if moment is None:
        return 1
    t.append(moment)
    valid_lat, valid_lon = _position(lat, lon)
    ch["lat"].append(valid_lat)
    ch["lon"].append(valid_lon)
    for name in ch:
        if name not in ("lat", "lon"):
            ch[name].append(values.get(name))
    return 0


_TCX_TAGS = {
    "AltitudeMeters": "altitude",
    "DistanceMeters": "distance",
    "Cadence": "cadence",
    "RunCadence": "cadence",
    "Watts": "power",
    "Speed": "speed",
}


def parse_tcx(data: bytes, *, strict: bool = True) -> RawActivity:
    """Decode TCX bytes into a :class:`RawActivity` (only tested with synthetic files)."""
    _check_xml(data)
    warnings: list[str] = []
    t: list[float] = []
    ch = empty_channels()
    sport: str | None = None
    try:
        for event, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = _local(el.tag)
            if event == "start" and tag == "Activity" and sport is None:
                sport = el.get("Sport")
            elif event == "end" and tag == "Trackpoint":
                _tcx_point(el, t, ch)
                el.clear()
    except ET.ParseError as err:
        if strict:
            raise IntegrityError(f"TCX XML damaged or truncated ({err})") from err
        warnings.append(f"TCX XML damaged or truncated ({err}): readable part used")
    if not t:
        raise BadFileError("TCX contains no track points")
    return RawActivity(
        source_format="tcx", t=t, channels=ch, sport_raw=sport, start=t[0], warnings=warnings
    )


def _tcx_point(el: ET.Element, t: list[float], ch: dict[str, list[Value]]) -> None:
    values: dict[str, Value] = {}
    moment: float | None = None
    parent_tags: dict[ET.Element, str] = {}
    for parent in el.iter():
        for child in parent:
            parent_tags[child] = _local(parent.tag)
    for child in el.iter():
        text = (child.text or "").strip()
        if not text:
            continue
        tag = _local(child.tag)
        if tag == "Time":
            moment = parse_time(text)
        elif tag == "LatitudeDegrees":
            values["lat"] = _number(text)
        elif tag == "LongitudeDegrees":
            values["lon"] = _number(text)
        elif tag == "Value" and parent_tags.get(child) == "HeartRateBpm":
            values["heart_rate"] = _number(text)
        elif tag in _TCX_TAGS:
            values[_TCX_TAGS[tag]] = _number(text)
    if moment is None:
        return
    t.append(moment)
    lat, lon = _position(values.get("lat"), values.get("lon"))
    values["lat"], values["lon"] = lat, lon
    for name in ch:
        ch[name].append(values.get(name))
