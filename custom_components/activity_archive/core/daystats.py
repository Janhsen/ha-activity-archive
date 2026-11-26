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
