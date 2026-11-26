"""Per-day values from hourly statistics rows (pure logic, no Home Assistant).

A dashboard card that shows one value per day does not need every hourly row: for most
sensors it needs the last value of the day, for some the mean of the hourly means. Doing that
reduction next to the database sends about one row per day instead of 24.

The rules are those of the card's own day aggregation, so values stay identical:

* ``last``: the last hourly row of the day that has a value; the value is the hourly ``mean``
  when there is one, otherwise the ``state``.
* ``mean``: the mean of the hourly ``mean`` values of the day (rows without a mean are skipped).

Nothing is interpolated: a day without a usable row is simply missing from the result.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable

# One hourly statistics row: (start as unix seconds, mean or None, state or None).
Row = tuple[float, float | None, float | None]


def last_per_day(rows: Iterable[Row], day_of: Callable[[float], str]) -> dict[str, float]:
    """Last usable value per local day. ``rows`` must be sorted by start time."""
    out: dict[str, float] = {}
    for start, mean, state in rows:
        value = mean if mean is not None else state
        if value is None:
            continue
        out[day_of(start)] = value
    return out


def mean_per_day(rows: Iterable[Row], day_of: Callable[[float], str]) -> dict[str, float]:
    """Mean of the hourly means per local day (rows without a mean are skipped)."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for start, mean, _state in rows:
        if mean is None:
            continue
        day = day_of(start)
        sums[day] = sums.get(day, 0.0) + mean
        counts[day] = counts.get(day, 0) + 1
    return {day: sums[day] / counts[day] for day in sums}


# One recorded state: (time as unix seconds, state text or None).
StateRow = tuple[float, str | None]

# ASCII digits and ASCII white space only, so that every match is a text float() accepts.
_NUMBER = re.compile(r"[ \t\n\r\f\v]*([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_NO_VALUE = {"", "unknown", "unavailable"}


def last_number_per_day(
    rows: Iterable[StateRow], day_of: Callable[[float], str]
) -> dict[str, float]:
    """Last numeric state per local day (by update time). ``rows`` must be sorted by time.

    Same reading as the card: the leading number of the text counts, anything else is skipped.
    """
    out: dict[str, float] = {}
    for when, state in rows:
        if state is None:
            continue
        found = _NUMBER.match(state)
        if found is None:
            continue
        value = float(found.group(1))
        if math.isfinite(value):
            out[day_of(when)] = value
    return out


def distinct_last_states(rows: Iterable[StateRow]) -> list[str]:
    """Each distinct state text once, at the position of its last occurrence.

    For states that are time stamps the card derives the day from the value, and the last row
    of a day wins. A value is always on the same day, so only its last occurrence can decide
    and the earlier copies can be dropped without changing the result.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for _when, state in reversed(list(rows)):
        if state is None or state in _NO_VALUE or state in seen:
            continue
        seen.add(state)
        kept.append(state)
    kept.reverse()
    return kept
