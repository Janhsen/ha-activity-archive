"""Synthetic activity data for the tests (marker: SYNTHETIC).

Everything here is generated from a fixed seed: there are no recordings of real people, places
or devices. Positions start near 0 N / 0 E (the Gulf of Guinea), dates are fixed (2000-01-01 and
the 2026 clock change night used for time zone tests).
"""

from __future__ import annotations

import gzip
import math
import random
import struct
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

FIT_EPOCH = 631065600
SEMICIRCLE = 180.0 / 2**31
T0 = 946_684_800 + 10 * 3600  # 2000-01-01T10:00:00Z
BASE_LAT, BASE_LON = 0.5, 0.5
METERS_PER_DEGREE = 111_320.0

Sample = dict[str, Any]

# FIT base types used below
ENUM, SINT8, UINT8, UINT16, SINT32, UINT32, UINT32Z = 0, 1, 2, 4, 5, 6, 12


def make_track(
    n: int,
    *,
    step: float = 1.0,
    start: float = T0,
    gps: bool = True,
    hr: bool = True,
    power: bool = False,
    cadence: bool = False,
    temperature: bool = True,
    hills: float = 0.0,
    wander: bool = False,
    seed: int = 7,
    noise_m: float = 0.0,
) -> list[Sample]:
    """Return ``n`` samples ``step`` seconds apart.

    ``wander`` makes the path curvy with strongly varying speed (the hard case for the GPS
    simplification); ``hills`` adds an elevation wave of that amplitude in metres.
    """
    rng = random.Random(seed)
    lat, lon, dist, heading = BASE_LAT, BASE_LON, 0.0, 0.7
    samples: list[Sample] = []
    for i in range(n):
        t = start + i * step
        if wander:
            heading += rng.gauss(0, 0.05)
            speed = max(0.5, 7 + 6 * math.sin(i / 90) + rng.gauss(0, 0.3))
        else:
            speed = 5 + 2 * math.sin(i / 200)
        dist += speed * step
        lat += speed * step * math.cos(heading) / METERS_PER_DEGREE
        lon += speed * step * math.sin(heading) / METERS_PER_DEGREE
        alt = 500 + i * step * 0.05 + hills * math.sin(i * step / 40) + rng.gauss(0, 0.2)
        sample: Sample = {
            "t": t,
            "lat": lat + rng.gauss(0, noise_m) / METERS_PER_DEGREE if gps else None,
            "lon": lon + rng.gauss(0, noise_m) / METERS_PER_DEGREE if gps else None,
            "hr": round(130 + 20 * math.sin(i / 300) + rng.gauss(0, 2)) if hr else None,
            "power": round(max(0.0, 200 + 80 * math.sin(i / 50))) if power else None,
            "cadence": round(85 + 5 * math.sin(i / 70)) if cadence else None,
            "dist": dist,
            "alt": alt,
            "speed": speed,
            "temp": 18 if temperature else None,
        }
        samples.append(sample)
    return samples


# ----------------------------------------------------------------------------- FIT encoder
def fit_crc(data: bytes) -> int:
    """Independent CRC-16 (nibble table) so the reader's CRC is not tested against itself."""
    table = [
        0x0000,
        0xCC01,
        0xD801,
        0x1400,
        0xF001,
        0x3C00,
        0x2800,
        0xE401,
        0xA001,
        0x6C00,
        0x7800,
        0xB401,
        0x5000,
        0x9C01,
        0x8801,
        0x4400,
    ]
    crc = 0
    for byte in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[byte & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
    return crc


def _definition(
    local: int,
    global_num: int,
    fields: Sequence[tuple[int, int, int]],
    dev_sizes: Sequence[int] = (),
) -> bytes:
    header = 0x40 | local | (0x20 if dev_sizes else 0)
    out = bytes([header, 0, 0]) + struct.pack("<HB", global_num, len(fields))
    for number, size, base in fields:
        out += bytes([number, size, base])
    if dev_sizes:
        out += bytes([len(dev_sizes)])
        for index, size in enumerate(dev_sizes):
            out += bytes([index, size, 0])
    return out


_RECORD_FIELDS = [
    (253, 4, UINT32),
    (0, 4, SINT32),
    (1, 4, SINT32),
    (3, 1, UINT8),
    (4, 1, UINT8),
    (5, 4, UINT32),
    (7, 2, UINT16),
    (13, 1, SINT8),
    (78, 4, UINT32),
    (73, 4, UINT32),
]


def _pack_record(sample: Sample, with_time: bool, dev_pad: int) -> bytes:
    def opt(value: float | None, code: str, invalid: int, scale: float = 1.0, off: float = 0.0):
        return struct.pack("<" + code, invalid if value is None else round((value + off) * scale))

    out = struct.pack("<I", int(sample["t"]) - FIT_EPOCH) if with_time else b""
    lat, lon = sample.get("lat"), sample.get("lon")
    out += opt(None if lat is None else lat / SEMICIRCLE, "i", 0x7FFFFFFF)
    out += opt(None if lon is None else lon / SEMICIRCLE, "i", 0x7FFFFFFF)
    out += opt(sample.get("hr"), "B", 0xFF)
    out += opt(sample.get("cadence"), "B", 0xFF)
    out += opt(sample.get("dist"), "I", 0xFFFFFFFF, 100)
    out += opt(sample.get("power"), "H", 0xFFFF)
    out += opt(sample.get("temp"), "b", 0x7F)
    out += opt(sample.get("alt"), "I", 0xFFFFFFFF, 5, 500)
    out += opt(sample.get("speed"), "I", 0xFFFFFFFF, 1000)
    return out + b"\x00" * dev_pad


def wrap_fit(body: bytes) -> bytes:
    """Put a 14 byte header and the trailing CRC around message bytes."""
    header = bytes([14, 0x20]) + struct.pack("<HI", 2100, len(body)) + b".FIT"
    header += struct.pack("<H", fit_crc(header))
    return header + body + struct.pack("<H", fit_crc(header + body))


def encode_fit(
    samples: Sequence[Sample],
    *,
    events: Iterable[tuple[float, int, int]] = (),
    session: dict[str, Any] | None = None,
    local_offset_s: int | None = None,
    serial: int | None = None,
    lengths: Iterable[dict[str, Any]] = (),
    developer_fields: bool = False,
    compressed: bool = False,
) -> bytes:
    """Build a valid FIT file.

    ``events`` are ``(time, event, event_type)``; ``session`` takes ``start``, ``end``, ``sport``,
    ``sub``, ``elapsed``, ``timer``, ``dist``, ``ascent``, ``descent``, ``avg_hr``, ``max_hr``.
    """
    body = b""
    if serial is not None:
        body += _definition(2, 0, [(0, 1, ENUM), (1, 2, UINT16), (3, 4, UINT32Z), (4, 4, UINT32)])
        body += bytes([2]) + struct.pack("<BHII", 4, 1, serial, int(samples[0]["t"]) - FIT_EPOCH)
    dev_sizes = (3,) if developer_fields else ()
    body += _definition(0, 20, _RECORD_FIELDS, dev_sizes)
    body += _definition(1, 20, _RECORD_FIELDS[1:])
    body += _definition(3, 21, [(253, 4, UINT32), (0, 1, ENUM), (1, 1, ENUM)])
    items: list[tuple[float, int, Any]] = [(s["t"], 0, s) for s in samples]
    items += [(t, 1, (ev, ty)) for t, ev, ty in events]
    if events:  # without events the sample order is kept as given (time jump tests)
        items.sort(key=lambda x: (x[0], x[1]))
    last: int | None = None
    for t, kind, payload in items:
        if kind == 1:
            body += bytes([3]) + struct.pack("<IBB", int(t) - FIT_EPOCH, *payload)
            continue
        ts = int(t) - FIT_EPOCH
        if compressed and last is not None and 0 <= ts - last < 32:
            body += bytes([0x80 | (1 << 5) | (ts & 0x1F)]) + _pack_record(payload, False, 0)
        else:
            body += bytes([0]) + _pack_record(payload, True, 3 if developer_fields else 0)
        last = ts
    for length in lengths:
        body += _definition(
            0,
            101,
            [(253, 4, UINT32), (2, 4, UINT32), (3, 4, UINT32), (4, 4, UINT32), (5, 2, UINT16)],
        )
        body += bytes([0]) + struct.pack(
            "<IIIIH",
            int(length["start"]) - FIT_EPOCH + 30,
            int(length["start"]) - FIT_EPOCH,
            int(length["elapsed"] * 1000),
            int(length["timer"] * 1000),
            length["strokes"],
        )
    if session is not None:
        s = session
        body += _definition(
            0,
            18,
            [
                (253, 4, UINT32),
                (2, 4, UINT32),
                (5, 1, ENUM),
                (6, 1, ENUM),
                (7, 4, UINT32),
                (8, 4, UINT32),
                (9, 4, UINT32),
                (22, 2, UINT16),
                (23, 2, UINT16),
                (16, 1, UINT8),
                (17, 1, UINT8),
            ],
        )
        body += bytes([0]) + struct.pack(
            "<IIBBIIIHHBB",
            int(s["end"]) - FIT_EPOCH,
            int(s["start"]) - FIT_EPOCH,
            s["sport"],
            s["sub"],
            int(s["elapsed"] * 1000),
            int(s["timer"] * 1000),
            int(s["dist"] * 100),
            s.get("ascent", 0),
            s.get("descent", 0),
            s.get("avg_hr", 0xFF),
            s.get("max_hr", 0xFF),
        )
    if local_offset_s is not None:
        end = int(samples[-1]["t"]) - FIT_EPOCH
        body += _definition(0, 34, [(253, 4, UINT32), (5, 4, UINT32)])
        body += bytes([0]) + struct.pack("<II", end, end + local_offset_s)
    return wrap_fit(body)


def session_for(samples: Sequence[Sample], sport: int, sub: int, **extra: Any) -> dict[str, Any]:
    """Session values matching ``samples`` (device totals are given by the caller, not computed)."""
    first, last = samples[0]["t"], samples[-1]["t"]
    base = {
        "start": first,
        "end": last,
        "sport": sport,
        "sub": sub,
        "elapsed": last - first,
        "timer": last - first,
        "dist": samples[-1]["dist"] or 0.0,
    }
    return base | extra


def corrupt_crc(data: bytes) -> bytes:
    """Flip the last byte (the file CRC)."""
    return data[:-1] + bytes([data[-1] ^ 0xFF])


# ----------------------------------------------------------------------------- GPX / TCX
def iso(t: float) -> str:
    """Format a Unix time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_GPX_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gpx creator="SYNTHETIC" xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">\n'
    "<trk><name>{name}</name><type>{kind}</type>\n"
)


def gpx_point(
    sample: Sample, *, ele: bool = True, ext: bool = True, time_text: str | None = None
) -> str:
    """One ``trkpt``; extensions carry heart rate, cadence and temperature when present."""
    parts = []
    if ele and sample.get("alt") is not None:
        parts.append(f"<ele>{sample['alt']:.1f}</ele>")
    parts.append(f"<time>{time_text or iso(sample['t'])}</time>")
    if ext:
        inner = ""
        for tag, key in (("atemp", "temp"), ("hr", "hr"), ("cad", "cadence")):
            if sample.get(key) is not None:
                inner += f"<gpxtpx:{tag}>{sample[key]}</gpxtpx:{tag}>"
        if inner:
            parts.append(
                f"<extensions><gpxtpx:TrackPointExtension>{inner}"
                "</gpxtpx:TrackPointExtension></extensions>"
            )
    return f'<trkpt lat="{sample["lat"]:.6f}" lon="{sample["lon"]:.6f}">{"".join(parts)}</trkpt>'


def encode_gpx(
    samples: Sequence[Sample],
    *,
    name: str = "Synthetic ride",
    kind: str = "cycling",
    ele: bool = True,
    ext: bool = True,
    split_at: Iterable[int] = (),
    time_texts: Sequence[str] | None = None,
) -> bytes:
    """Build a GPX 1.1 file; ``split_at`` starts a new ``trkseg`` before those sample indexes."""
    splits = set(split_at)
    out = _GPX_HEAD.format(name=name, kind=kind) + "<trkseg>\n"
    for i, sample in enumerate(samples):
        if i in splits:
            out += "</trkseg><trkseg>\n"
        text = time_texts[i] if time_texts else None
        out += gpx_point(sample, ele=ele, ext=ext, time_text=text) + "\n"
    return (out + "</trkseg></trk></gpx>\n").encode()


def encode_tcx(samples: Sequence[Sample], *, sport: str = "Biking") -> bytes:
    """Build a small TCX file with position, altitude, distance and heart rate."""
    points = ""
    for s in samples:
        points += (
            f"<Trackpoint><Time>{iso(s['t'])}</Time><Position><LatitudeDegrees>{s['lat']:.6f}"
            f"</LatitudeDegrees><LongitudeDegrees>{s['lon']:.6f}</LongitudeDegrees></Position>"
            f"<AltitudeMeters>{s['alt']:.1f}</AltitudeMeters><DistanceMeters>{s['dist']:.1f}"
            f"</DistanceMeters><HeartRateBpm><Value>{s['hr']}</Value></HeartRateBpm></Trackpoint>"
        )
    return (
        '<?xml version="1.0"?><TrainingCenterDatabase xmlns='
        '"http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"><Activities>'
        f'<Activity Sport="{sport}"><Lap><Track>{points}</Track></Lap></Activity>'
        "</Activities></TrainingCenterDatabase>"
    ).encode()


def gzip_bytes(data: bytes) -> bytes:
    """Gzip with a fixed header so the output is reproducible."""
    return gzip.compress(data, mtime=0)
