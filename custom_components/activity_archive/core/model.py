"""Plain data model shared by readers, simplification, de-duplication and writer.

Nothing in here imports Home Assistant. All times are Unix seconds in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Raw channel names a reader may fill. ``lat``/``lon`` are degrees, everything else SI-like:
# heart_rate bpm, power W, cadence rpm (FIT value, not yet normalised), temperature degC,
# altitude m, speed m/s, distance m (cumulative).
CHANNELS: Final[tuple[str, ...]] = (
    "heart_rate",
    "power",
    "cadence",
    "temperature",
    "altitude",
    "speed",
    "distance",
    "lat",
    "lon",
)

Value = float | None


class ReaderError(Exception):
    """A file could not be read; the message is safe to show in a report."""


class BadFileError(ReaderError):
    """The bytes are not a usable activity file (wrong format, empty, no records)."""


class IntegrityError(BadFileError):
    """The file is damaged (CRC mismatch, truncated); it is skipped and reported."""


class UnsafeFileError(BadFileError):
    """The file was rejected for safety reasons (decompression bomb, XML entities)."""


def empty_channels() -> dict[str, list[Value]]:
    """Return a fresh mapping with one empty list per raw channel."""
    return {name: [] for name in CHANNELS}


@dataclass(slots=True)
class RawActivity:
    """What a reader found in one file: parallel sample arrays plus session scalars.

    ``session`` only holds values the source states itself (FIT session message), never anything
    computed from the samples. ``pauses`` are ``(start, end)`` intervals in which the timer was
    stopped; samples inside them are dropped before simplification.
    """

    source_format: str
    t: list[float]
    channels: dict[str, list[Value]]
    sport_raw: str | None = None
    sub_sport_raw: str | None = None
    start: float | None = None
    tz_offset_s: int | None = None
    tz_source: str = "assumed"
    pauses: list[tuple[float, float]] = field(default_factory=list)
    session: dict[str, float | int | str] = field(default_factory=dict)
    lengths: list[dict[str, float | int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    title: str | None = None
    file_id: tuple[int, ...] | None = None
    content_hash: str | None = None

    def has(self, channel: str) -> bool:
        """Return True if at least one sample of the channel has a value."""
        return any(v is not None for v in self.channels.get(channel, ()))


@dataclass(frozen=True, slots=True)
class CurveSeries:
    """One scalar curve ready to be written: ``(timestamp, value)`` points plus gap markers.

    ``markers`` are timestamps at which the state ``unavailable`` is written so that history
    charts end the line (after the last point and after every gap).
    """

    key: str
    unit: str
    step_s: int
    points: tuple[tuple[int, float], ...]
    markers: tuple[int, ...] = ()
    decimals: int = 0


@dataclass(frozen=True, slots=True)
class GpsTrack:
    """The simplified GPS line: ``(timestamp, lat, lon)`` plus gap markers."""

    points: tuple[tuple[int, float, float], ...]
    markers: tuple[int, ...] = ()
    epsilon_m: float = 5.0
    time_epsilon_m: float | None = None
    max_gap_s: int = 30
    raw_points: int = 0
    spikes_removed: int = 0


@dataclass(frozen=True, slots=True)
class StatePoint:
    """One state to be put into the recorder at ``ts`` (Unix seconds)."""

    ts: int
    state: str
    attributes: tuple[tuple[str, object], ...] = ()

    def attrs(self) -> dict[str, object]:
        """Return the attributes as a dict."""
        return dict(self.attributes)
