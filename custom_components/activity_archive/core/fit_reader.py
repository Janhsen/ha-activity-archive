"""Minimal Garmin FIT reader using only the standard library.

It decodes exactly what the archive needs: ``record`` (samples), ``event`` (timer start/stop),
``session`` (the device's own totals), ``activity`` (local time offset), ``length`` (pool
lengths) and ``file_id`` (identity for duplicate detection). Everything else is skipped by size.

Integrity: the header CRC and the CRC of every (chained) file are checked. In ``strict`` mode,
which the importer uses, a CRC mismatch or a truncated file raises :class:`IntegrityError` and
the file is skipped and reported; with ``strict=False`` the readable part is returned together
with a warning.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from itertools import pairwise
from typing import Any, Final

from . import sports
from .model import (
    BadFileError,
    IntegrityError,
    RawActivity,
    Value,
    empty_channels,
)

FIT_EPOCH: Final = 631065600  # 1989-12-31T00:00:00Z as Unix time
SEMICIRCLE_TO_DEG: Final = 180.0 / 2**31
_MIN_ABSOLUTE_TS: Final = 0x10000000  # smaller FIT timestamps are device uptime, not UTC

# FIT base type number -> (struct code, size, invalid value or None)
_BASE_TYPES: Final[dict[int, tuple[str, int, int | None]]] = {
    0: ("B", 1, 0xFF),
    1: ("b", 1, 0x7F),
    2: ("B", 1, 0xFF),
    3: ("h", 2, 0x7FFF),
    4: ("H", 2, 0xFFFF),
    5: ("i", 4, 0x7FFFFFFF),
    6: ("I", 4, 0xFFFFFFFF),
    7: ("s", 1, None),
    8: ("f", 4, None),
    9: ("d", 8, None),
    10: ("B", 1, 0),
    11: ("H", 2, 0),
    12: ("I", 4, 0),
    13: ("B", 1, 0xFF),
    14: ("q", 8, 0x7FFFFFFFFFFFFFFF),
    15: ("Q", 8, 0xFFFFFFFFFFFFFFFF),
    16: ("Q", 8, 0),
}

_MSG_FILE_ID: Final = 0
_MSG_SESSION: Final = 18
_MSG_RECORD: Final = 20
_MSG_EVENT: Final = 21
_MSG_ACTIVITY: Final = 34
_MSG_LENGTH: Final = 101

# global message number -> {field number: name}
_FIELDS: Final[dict[int, dict[int, str]]] = {
    _MSG_FILE_ID: {0: "type", 1: "manufacturer", 2: "product", 3: "serial", 4: "created"},
    _MSG_RECORD: {
        253: "ts",
        0: "lat",
        1: "lon",
        2: "alt0",
        3: "hr",
        4: "cad",
        5: "dist",
        6: "spd0",
        7: "pwr",
        13: "temp",
        73: "spd",
        78: "alt",
        53: "cadfrac",
    },
    _MSG_EVENT: {253: "ts", 0: "event", 1: "type"},
    _MSG_SESSION: {
        253: "ts",
        2: "start",
        5: "sport",
        6: "sub_sport",
        7: "elapsed",
        8: "timer",
        9: "dist",
        11: "kcal",
        14: "avg_spd0",
        15: "max_spd0",
        124: "avg_spd",
        125: "max_spd",
        16: "avg_hr",
        17: "max_hr",
        18: "avg_cad",
        19: "max_cad",
        20: "avg_pwr",
        21: "max_pwr",
        22: "ascent",
        23: "descent",
        57: "avg_temp",
    },
    _MSG_ACTIVITY: {253: "ts", 5: "local_ts"},
    _MSG_LENGTH: {253: "ts", 2: "start", 3: "elapsed", 4: "timer", 5: "strokes", 12: "ltype"},
}

# Timer event types that stop / restart the clock (event 0 = timer).
_STOP_TYPES: Final = frozenset({1, 4, 8, 9})
_START_TYPES: Final = frozenset({0, 5})

Record = dict[str, Any]
_Plan = list[tuple[int, str | None, int | None, int]]
_Definition = tuple[int, struct.Struct, _Plan, int]


def _build_crc_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE: Final = _build_crc_table()


def fit_crc(data: bytes | bytearray | memoryview) -> int:
    """Return the 16 bit FIT CRC of ``data``."""
    crc = 0
    table = _CRC_TABLE
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc


def _integrity(strict: bool, warn: list[str], message: str) -> None:
    if strict:
        raise IntegrityError(message)
    warn.append(message + ": readable part used")


def _iter_files(data: bytes, strict: bool, warn: list[str]) -> Iterator[tuple[int, Record]]:
    """Yield ``(global message number, fields)`` from all chained FIT files in ``data``."""
    pos0 = 0
    count = 0
    while pos0 + 12 <= len(data):
        header_size = data[pos0]
        if header_size not in (12, 14) or data[pos0 + 8 : pos0 + 12] != b".FIT":
            if count == 0:
                raise BadFileError("not a FIT file (header missing)")
            warn.append(f"ignored {len(data) - pos0} trailing bytes after the last FIT file")
            return
        if header_size == 14:
            stored = struct.unpack_from("<H", data, pos0 + 12)[0]
            if stored and stored != fit_crc(data[pos0 : pos0 + 12]):
                _integrity(strict, warn, "FIT header CRC mismatch")
        data_size = struct.unpack_from("<I", data, pos0 + 4)[0]
        start = pos0 + header_size
        end = start + data_size
        if end + 2 > len(data):
            have = len(data) - start
            _integrity(strict, warn, f"FIT file truncated ({have} of {data_size} bytes)")
            end = len(data)
        elif fit_crc(memoryview(data)[pos0:end]) != struct.unpack_from("<H", data, end)[0]:
            _integrity(strict, warn, "FIT CRC mismatch")
        yield from _iter_messages(data, start, end, warn)
        count += 1
        pos0 = end + 2


def _iter_messages(
    data: bytes, pos: int, end: int, warn: list[str]
) -> Iterator[tuple[int, Record]]:
    definitions: dict[int, _Definition] = {}
    last_ts: int | None = None
    while pos < end:
        header = data[pos]
        pos += 1
        compressed = bool(header & 0x80)
        if compressed:
            local = (header >> 5) & 3
        elif header & 0x40:
            try:
                pos = _read_definition(data, pos, header, header & 15, definitions)
            except (IndexError, struct.error):
                warn.append("FIT definition truncated")
                return
            continue
        else:
            local = header & 15
        definition = definitions.get(local)
        if definition is None:
            warn.append(f"FIT data message without definition at byte {pos}")
            return
        global_num, layout, plan, total = definition
        if pos + total > end:
            warn.append("FIT last data message truncated")
            return
        wanted = _FIELDS.get(global_num)
        if wanted is None:
            pos += total
            continue
        values = layout.unpack_from(data, pos)
        pos += total
        rec: Record = {}
        for (number, code, invalid, base), raw in zip(plan, values, strict=False):
            name = wanted.get(number)
            if name is None:
                continue
            if code is None:
                if base == 7:
                    text = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
                    if text:
                        rec[name] = text
                continue
            if invalid is not None and raw == invalid:
                continue
            if code in "fd" and raw != raw:  # NaN
                continue
            rec[name] = raw
        if compressed and last_ts is not None:
            offset = header & 0x1F
            ts = (last_ts & ~0x1F) + offset
            if offset < (last_ts & 0x1F):
                ts += 32
            rec["ts"] = last_ts = ts
        elif not compressed and rec.get("ts", 0) >= _MIN_ABSOLUTE_TS:
            last_ts = rec["ts"]
        yield global_num, rec


def _read_definition(
    data: bytes, pos: int, header: int, local: int, definitions: dict[int, _Definition]
) -> int:
    """Parse a definition message at ``pos`` (after its header byte); return the next position."""
    order = ">" if data[pos + 1] == 1 else "<"
    global_num = struct.unpack_from(order + "H", data, pos + 2)[0]
    field_count = data[pos + 4]
    p = pos + 5
    layout = order
    plan: _Plan = []
    total = 0
    for i in range(field_count):
        number, size, base = data[p + 3 * i], data[p + 3 * i + 1], data[p + 3 * i + 2] & 0x1F
        code, base_size, invalid = _BASE_TYPES.get(base, ("B", 1, 0xFF))
        if base == 7 or size != base_size:  # strings and arrays are skipped as bytes
            layout += f"{size}s"
            plan.append((number, None, None, base))
        else:
            layout += code
            plan.append((number, code, invalid, base))
        total += size
    p += 3 * field_count
    if header & 0x20:  # developer fields: only their sizes matter
        dev_count = data[p]
        p += 1
        for i in range(dev_count):
            size = data[p + 3 * i + 1]
            layout += f"{size}s"
            total += size
        p += 3 * dev_count
    definitions[local] = (global_num, struct.Struct(layout), plan, total)
    return p


def _scaled(value: float | None, scale: float, offset: float = 0.0) -> float | None:
    return None if value is None else value / scale - offset


def parse_fit(data: bytes, *, strict: bool = True) -> RawActivity:
    """Decode FIT bytes into a :class:`RawActivity`.

    Raises :class:`BadFileError` for non-FIT or empty files and :class:`IntegrityError` for CRC
    errors or truncation when ``strict`` is true.
    """
    warnings: list[str] = []
    records: list[Record] = []
    events: list[Record] = []
    sessions: list[Record] = []
    lengths: list[Record] = []
    activity: Record = {}
    file_id: Record = {}
    for number, rec in _iter_files(data, strict, warnings):
        if number == _MSG_RECORD:
            records.append(rec)
        elif number == _MSG_EVENT:
            events.append(rec)
        elif number == _MSG_SESSION:
            sessions.append(rec)
        elif number == _MSG_LENGTH:
            lengths.append(rec)
        elif number == _MSG_ACTIVITY:
            activity.update(rec)
        elif number == _MSG_FILE_ID and not file_id:
            file_id = rec
    if not records and not sessions:
        raise BadFileError("FIT file contains no records and no session")

    t: list[float] = []
    ch = empty_channels()
    skipped = 0
    for rec in records:
        ts = rec.get("ts")
        if ts is None or ts < _MIN_ABSOLUTE_TS:
            skipped += 1
            continue
        t.append(float(ts + FIT_EPOCH))
        lat, lon = rec.get("lat"), rec.get("lon")
        valid_pos = lat is not None and lon is not None and not (lat == 0 and lon == 0)
        ch["lat"].append(lat * SEMICIRCLE_TO_DEG if valid_pos else None)
        ch["lon"].append(lon * SEMICIRCLE_TO_DEG if valid_pos else None)
        ch["altitude"].append(_scaled(rec.get("alt", rec.get("alt0")), 5, 500))
        ch["speed"].append(_scaled(rec.get("spd", rec.get("spd0")), 1000))
        ch["distance"].append(_scaled(rec.get("dist"), 100))
        ch["heart_rate"].append(rec.get("hr"))
        cadence = rec.get("cad")
        if cadence is not None and "cadfrac" in rec:
            cadence = cadence + rec["cadfrac"] / 128
        ch["cadence"].append(cadence)
        ch["power"].append(rec.get("pwr"))
        ch["temperature"].append(rec.get("temp"))
    if skipped:
        warnings.append(f"{skipped} records without an absolute time stamp ignored")
    if any(b < a for a, b in pairwise(t)):
        warnings.append("time stamps not monotonic: samples sorted")
        order = sorted(range(len(t)), key=t.__getitem__)
        t = [t[i] for i in order]
        ch = {k: [v[i] for i in order] for k, v in ch.items()}

    first = sessions[0] if sessions else {}
    if len(sessions) > 1:
        warnings.append("several sessions: device totals omitted")
    sport, sub_sport = sports.fit_names(first.get("sport"), first.get("sub_sport"))
    session = _session_scalars(first) if len(sessions) == 1 else {}
    start = first["start"] + FIT_EPOCH if "start" in first else (t[0] if t else None)

    tz_offset: int | None = None
    tz_source = "assumed"
    if "local_ts" in activity and "ts" in activity:
        delta = activity["local_ts"] - activity["ts"]
        if abs(delta) <= 14 * 3600 and delta % 900 == 0:
            tz_offset, tz_source = delta, "fit"
        else:
            warnings.append("implausible local time offset ignored")

    ident = None
    if file_id.get("serial") and file_id.get("created"):
        ident = (
            int(file_id.get("manufacturer", 0)),
            int(file_id.get("product", 0)),
            int(file_id["serial"]),
            int(file_id["created"]),
        )
    return RawActivity(
        source_format="fit",
        t=t,
        channels=ch,
        sport_raw=sport,
        sub_sport_raw=sub_sport,
        start=start,
        tz_offset_s=tz_offset,
        tz_source=tz_source,
        pauses=_pauses(events, t),
        session=session,
        lengths=[_length(x) for x in lengths],
        warnings=warnings,
        file_id=ident,
    )


def _session_scalars(s: Record) -> dict[str, float | int | str]:
    """Return the device's own totals; a missing value stays missing."""
    out: dict[str, float | int | str] = {}

    def put(name: str, value: Value) -> None:
        if value is not None:
            out[name] = value

    put("elapsed_s", _scaled(s.get("elapsed"), 1000))
    put("timer_s", _scaled(s.get("timer"), 1000))
    put("distance_m", _scaled(s.get("dist"), 100))
    put("ascent_m", s.get("ascent"))
    put("descent_m", s.get("descent"))
    put("calories", s.get("kcal"))
    put("avg_hr", s.get("avg_hr"))
    put("max_hr", s.get("max_hr"))
    put("avg_cadence", s.get("avg_cad"))
    put("max_cadence", s.get("max_cad"))
    put("avg_power", s.get("avg_pwr"))
    put("max_power", s.get("max_pwr"))
    put("avg_speed_mps", _scaled(s.get("avg_spd", s.get("avg_spd0")), 1000))
    put("max_speed_mps", _scaled(s.get("max_spd", s.get("max_spd0")), 1000))
    put("avg_temp", s.get("avg_temp"))
    return out


def _length(rec: Record) -> dict[str, float | int]:
    out: dict[str, float | int] = {}
    if "start" in rec:
        out["start"] = rec["start"] + FIT_EPOCH
    for key in ("elapsed", "timer"):
        if key in rec:
            out[key + "_s"] = rec[key] / 1000
    if "strokes" in rec:
        out["strokes"] = rec["strokes"]
    return out


def _pauses(events: list[Record], t: list[float]) -> list[tuple[float, float]]:
    """Intervals from a timer stop to the next start; an open stop ends after the last sample."""
    pauses: list[tuple[float, float]] = []
    stop: float | None = None
    timer = sorted((e for e in events if e.get("event") == 0 and "ts" in e), key=lambda e: e["ts"])
    for event in timer:
        when = float(event["ts"] + FIT_EPOCH)
        kind = event.get("type")
        if kind in _STOP_TYPES and stop is None:
            stop = when
        elif kind in _START_TYPES and stop is not None:
            if when > stop:
                pauses.append((stop, when))
            stop = None
    if stop is not None and t and t[-1] > stop:
        pauses.append((stop, t[-1] + 1))
    return pauses
