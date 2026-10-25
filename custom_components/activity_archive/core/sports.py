"""Sport normalisation: map source specific sport names to a small English slug list."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

_TABLE_PATH = Path(__file__).with_name("sports.json")

# Sports whose FIT cadence is stored per leg; the archive stores steps per minute.
STEP_CADENCE_SPORTS = frozenset({"running", "walking", "hiking"})


@dataclass(frozen=True, slots=True)
class SportInfo:
    """Normalised sport. ``raw`` keeps the source wording."""

    sport: str
    sport_type: str | None
    family: str
    raw: str

    @property
    def trainer(self) -> bool:
        """Return True for indoor/treadmill variants."""
        return self.sport_type in {"indoor", "treadmill"}

    @property
    def cadence_unit(self) -> str:
        """Unit of the cadence curve: steps per minute on foot, revolutions per minute else."""
        return "spm" if self.sport in STEP_CADENCE_SPORTS else "rpm"

    @property
    def uses_time_position(self) -> bool:
        """Cycling gets the additional time-synchronous GPS limit."""
        return self.family == "cycling"


@cache
def _table() -> dict[str, Any]:
    with _TABLE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def fit_names(sport: int | None, sub_sport: int | None) -> tuple[str | None, str | None]:
    """Return the FIT enum names for the numeric ``sport``/``sub_sport`` (unknown stays None)."""
    table = _table()
    s = table["fit_sport"].get(str(sport)) if sport is not None else None
    sub = table["fit_sub_sport"].get(str(sub_sport)) if sub_sport is not None else None
    return s, sub


def _info(pair: list[str | None], raw: str) -> SportInfo:
    sport, sport_type = pair
    family = _table()["families"].get(sport or "other", "other")
    return SportInfo(sport or "other", sport_type, family, raw)


def from_strava(activity_type: str | None, *, csv: bool = False) -> SportInfo:
    """Normalise a Strava ``sport_type``; unknown values become ``other``.

    The export CSV only knows the coarse ``Activity Type``: its ``Ride`` may be a road, gravel or
    mountain bike ride, so with ``csv=True`` a ``Ride`` carries no sport type.
    """
    raw = (activity_type or "").strip()
    pair = _table()["strava"].get(raw)
    info = _info(pair or ["other", None], raw)
    if csv and raw == "Ride":
        info = SportInfo(info.sport, None, info.family, raw)
    return info


def from_fit(sport: str | None, sub_sport: str | None) -> SportInfo:
    """Normalise FIT enum names (``cycling``, ``mountain``); unknown values become ``other``."""
    raw = "/".join(x for x in (sport, sub_sport) if x and x != "generic")
    table = _table()["fit"]
    pair = table.get(raw) or table.get(sport or "")
    if pair is None and sport in {"fitness_equipment", "training"} and sub_sport:
        pair = table.get(f"training/{sub_sport}")
    return _info(pair or ["other", None], raw)


def from_gpx_type(type_text: str | None) -> SportInfo:
    """GPX carries no reliable sport; the archive records ``other`` and keeps the raw text."""
    return _info(["other", None], (type_text or "").strip())
