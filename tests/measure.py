"""Measurement helpers for the tests (not part of the integration)."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise


def ascent_hysteresis(values: Sequence[float | None], threshold: float = 3.0) -> float:
    """Total ascent counting only confirmed climbs of at least ``threshold`` metres."""
    vals = [v for v in values if v is not None]
    if not vals:
        return 0.0
    turning = [vals[0]]
    mode, extreme = 0, vals[0]
    for v in vals[1:]:
        if mode == 0:
            if v - turning[0] >= threshold:
                mode, extreme = 1, v
            elif turning[0] - v >= threshold:
                mode, extreme = -1, v
        elif mode == 1:
            if v > extreme:
                extreme = v
            elif extreme - v >= threshold:
                turning.append(extreme)
                mode, extreme = -1, v
        elif v < extreme:
            extreme = v
        elif v - extreme >= threshold:
            turning.append(extreme)
            mode, extreme = 1, v
    turning.append(extreme)
    return sum(max(0.0, b - a) for a, b in pairwise(turning))


def time_weighted_mean(t: Sequence[float], values: Sequence[float | None]) -> float:
    """Mean of a step signal: each value counts until the next sample."""
    total = weight = 0.0
    for i in range(len(t) - 1):
        if values[i] is None:
            continue
        dt = t[i + 1] - t[i]
        total += values[i] * dt
        weight += dt
    return total / weight
