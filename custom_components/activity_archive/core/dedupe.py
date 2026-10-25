"""Decide whether two records describe the same activity, and which source provides what.

Decision tree (thresholds from the owner, checked later against the real export):

1. Same source and source id, or same file content hash: identical.
2. Same FIT ``file_id`` (manufacturer, product, serial, creation time): identical.
3. Start within +/-120 s? If not, an overlap of at least 90 % of the shorter interval with a
   matching pace makes a *crop candidate* (unclear), otherwise the records differ.
4. Duration within +/-5 % and, when both have a distance, distance within +/-3 %. Without
   a distance, start, duration and the same sport family decide.
5. The sport family is only a cross-check: Ride later changed to MountainBikeRide is the same
   family. Different families (cycling vs. running) make the pair unclear.
6. Anything just outside the limits is unclear. Unclear and different pairs both keep both
   records; unclear ones are listed for review. A duplicate is cheaper than a lost run.

Records of different people are never compared; the caller passes one person at a time.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations

from ..const import (
    DEDUPE_DISTANCE_TOLERANCE,
    DEDUPE_DURATION_TOLERANCE,
    DEDUPE_START_TOLERANCE_S,
)
from .sports import SportInfo

# Source kinds, best first: this is the order in which curves are taken.
GARMIN_FIT = "garmin_fit"
STRAVA_FILE = "strava_file"  # Strava export with the original upload (FIT/GPX/TCX)
STRAVA_API = "strava_api"
STRAVA_CSV = "strava_csv"  # export summary only, no curves
SOURCE_PRIORITY: dict[str, int] = {GARMIN_FIT: 0, STRAVA_FILE: 1, STRAVA_API: 2, STRAVA_CSV: 3}
STRAVA_SOURCES = frozenset({STRAVA_FILE, STRAVA_API, STRAVA_CSV})
FORMAT_PRIORITY: dict[str, int] = {"fit": 0, "tcx": 1, "gpx": 2}


class Decision(StrEnum):
    """Outcome of comparing two records."""

    IDENTICAL = "identical"
    DIFFERENT = "different"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class DedupeConfig:
    """Thresholds; the minimum slack values are open questions and default to zero."""

    start_tolerance_s: float = DEDUPE_START_TOLERANCE_S
    duration_tolerance: float = DEDUPE_DURATION_TOLERANCE
    distance_tolerance: float = DEDUPE_DISTANCE_TOLERANCE
    min_duration_slack_s: float = 0.0
    min_distance_slack_m: float = 0.0
    overlap_min: float = 0.9
    borderline_factor: float = 2.0
    crop_pace_tolerance: float = 0.25


@dataclass(frozen=True, slots=True)
class ActivityFacts:
    """What the comparison needs to know about one record from one source."""

    key: str
    source: str
    start: float
    source_id: str | None = None
    content_hash: str | None = None
    file_id: tuple[int, ...] | None = None
    duration_s: float | None = None
    distance_m: float | None = None
    sport: SportInfo | None = None
    file_format: str | None = None
    curves: frozenset[str] = frozenset()
    title: str | None = None
    description: str | None = None

    @property
    def family(self) -> str:
        """Sport family, ``other`` when unknown."""
        return self.sport.family if self.sport else "other"

    @property
    def end(self) -> float:
        """End time; equals the start when the duration is unknown."""
        return self.start + (self.duration_s or 0.0)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Comparison result with a human readable reason for the report."""

    decision: Decision
    reason: str
    notes: tuple[str, ...] = ()


def _relative(a: float, b: float) -> float:
    return abs(a - b) / max(a, b) if max(a, b) > 0 else 0.0


def _family_conflict(a: ActivityFacts, b: ActivityFacts) -> bool:
    return a.family != b.family and "other" not in (a.family, b.family)


def compare(a: ActivityFacts, b: ActivityFacts, cfg: DedupeConfig = DedupeConfig()) -> Verdict:  # noqa: B008
    """Compare two records. The result does not depend on the argument order."""
    if a.source == b.source and a.source_id and a.source_id == b.source_id:
        return Verdict(Decision.IDENTICAL, "same source id")
    if a.content_hash and a.content_hash == b.content_hash:
        return Verdict(Decision.IDENTICAL, "same file content")
    if a.file_id and a.file_id == b.file_id:
        return Verdict(Decision.IDENTICAL, "same FIT file id")

    start_diff = abs(a.start - b.start)
    dist_a, dist_b = a.distance_m or 0.0, b.distance_m or 0.0
    both_dist = dist_a > 0 and dist_b > 0
    if start_diff > cfg.start_tolerance_s:
        return _crop_or_different(a, b, cfg, both_dist)
    if not a.duration_s or not b.duration_s:
        return Verdict(Decision.UNCLEAR, "start matches but a duration is missing")

    duration_limit = max(
        cfg.duration_tolerance * max(a.duration_s, b.duration_s), cfg.min_duration_slack_s
    )
    duration_diff = abs(a.duration_s - b.duration_s)
    notes: list[str] = []
    if (
        a.sport
        and b.sport
        and a.sport.sport_type != b.sport.sport_type
        and not _family_conflict(a, b)
    ):
        notes.append(
            f"sport changed ({a.sport.raw or a.sport.sport} / {b.sport.raw or b.sport.sport})"
        )

    ratios = [duration_diff / duration_limit if duration_limit else float("inf")]
    if both_dist:
        distance_limit = max(cfg.distance_tolerance * max(dist_a, dist_b), cfg.min_distance_slack_m)
        ratios.append(abs(dist_a - dist_b) / distance_limit)
    worst = max(ratios)
    if worst <= 1.0:
        if _family_conflict(a, b):
            return Verdict(Decision.UNCLEAR, "numbers match but the sport families differ")
        reason = (
            "start, duration and distance match"
            if both_dist
            else ("start and duration match (no distance)")
        )
        return Verdict(Decision.IDENTICAL, reason, tuple(notes))
    if worst <= cfg.borderline_factor:
        return Verdict(Decision.UNCLEAR, "duration or distance just outside the limits")
    return Verdict(Decision.DIFFERENT, "duration or distance differ clearly")


def _crop_or_different(
    a: ActivityFacts, b: ActivityFacts, cfg: DedupeConfig, both_dist: bool
) -> Verdict:
    if a.duration_s and b.duration_s:
        shorter = min(a.duration_s, b.duration_s)
        overlap = min(a.end, b.end) - max(a.start, b.start)
        if overlap >= cfg.overlap_min * shorter:
            if both_dist:
                short, long_ = (a, b) if a.duration_s <= b.duration_s else (b, a)
                pace_short = (short.distance_m or 0.0) / (short.duration_s or 1.0)
                pace_long = (long_.distance_m or 0.0) / (long_.duration_s or 1.0)
                fits = (short.distance_m or 0.0) <= (long_.distance_m or 0.0) * (
                    1 + cfg.distance_tolerance
                )
                if fits and _relative(pace_short, pace_long) <= cfg.crop_pace_tolerance:
                    return Verdict(Decision.UNCLEAR, "crop candidate (overlapping, same pace)")
            elif not _family_conflict(a, b):
                return Verdict(Decision.UNCLEAR, "crop candidate (overlapping, no distance)")
    return Verdict(Decision.DIFFERENT, "start times differ")


@dataclass(frozen=True, slots=True)
class ReviewPair:
    """Two records the rules could not decide: both stay, the report lists them."""

    first: str
    second: str
    reason: str


@dataclass(frozen=True, slots=True)
class Group:
    """Records judged to be the same activity (one member is a group of its own)."""

    members: tuple[ActivityFacts, ...]


@dataclass(frozen=True, slots=True)
class Grouping:
    """Result of :func:`group_activities`."""

    groups: tuple[Group, ...]
    review: tuple[ReviewPair, ...]


def group_activities(
    records: Iterable[ActivityFacts],
    cfg: DedupeConfig = DedupeConfig(),  # noqa: B008
) -> Grouping:
    """Group records of one person into activities. The result is independent of input order.

    Identical pairs are merged (union-find). If a merged group contains a pair that is clearly
    different, nothing in it is merged and the identical pairs go to the review list instead.
    """
    ordered = sorted(records, key=lambda r: (r.start, r.key))
    parent = {r.key: r.key for r in ordered}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    verdicts: dict[tuple[str, str], Verdict] = {}
    for i, a in enumerate(ordered):
        reach = max(cfg.start_tolerance_s, a.duration_s or 0.0)
        for b in ordered[i + 1 :]:
            if b.start - a.start > reach:
                break
            verdicts[(a.key, b.key)] = compare(a, b, cfg)
            if verdicts[(a.key, b.key)].decision is Decision.IDENTICAL:
                parent[find(a.key)] = find(b.key)

    components: dict[str, list[ActivityFacts]] = {}
    for r in ordered:
        components.setdefault(find(r.key), []).append(r)

    review = [
        ReviewPair(a, b, v.reason)
        for (a, b), v in verdicts.items()
        if v.decision is Decision.UNCLEAR
    ]
    groups: list[Group] = []
    for members in components.values():
        if len(members) > 1 and _has_conflict(members, verdicts, cfg):
            for m in members:
                groups.append(Group((m,)))
            review += [
                ReviewPair(a, b, "identical pair inside a group with a conflict")
                for (a, b), v in verdicts.items()
                if v.decision is Decision.IDENTICAL
                and a in {m.key for m in members}
                and b in {m.key for m in members}
            ]
        else:
            groups.append(Group(tuple(members)))
    groups.sort(key=lambda g: (g.members[0].start, g.members[0].key))
    return Grouping(tuple(groups), tuple(sorted(review, key=lambda p: (p.first, p.second))))


def _has_conflict(
    members: Sequence[ActivityFacts], verdicts: dict[tuple[str, str], Verdict], cfg: DedupeConfig
) -> bool:
    for a, b in combinations(members, 2):
        verdict = verdicts.get((a.key, b.key)) or verdicts.get((b.key, a.key)) or compare(a, b, cfg)
        if verdict.decision is Decision.DIFFERENT:
            return True
    return False


# ---------------------------------------------------------------- source rule
@dataclass(frozen=True, slots=True)
class CanonicalActivity:
    """The merged result for one group: who provides which curve, and which metadata wins."""

    primary: str
    curve_sources: dict[str, str]
    title: str | None
    description: str | None
    sport: SportInfo | None
    aliases: tuple[tuple[str, str], ...]
    member_keys: tuple[str, ...]
    notes: tuple[str, ...] = field(default=())


def _rank(r: ActivityFacts) -> tuple[int, int, float, str]:
    return (
        SOURCE_PRIORITY.get(r.source, 99),
        FORMAT_PRIORITY.get(r.file_format or "", 9),
        -(r.duration_s or 0.0),
        r.key,
    )


def merge_group(group: Group) -> CanonicalActivity:
    """Apply the source rule to a group.

    * Curves come from the best source; a curve kind it lacks may come from a lower one.
    * Title, description and sport come from Strava when a Strava record has them. A Strava
      sport without a sub type (CSV ``Ride``) does not erase a more specific sub type of another
      record of the same sport.
    """
    members = sorted(group.members, key=_rank)
    primary = members[0]
    curve_sources: dict[str, str] = {}
    for member in members:
        for curve in sorted(member.curves):
            curve_sources.setdefault(curve, member.key)
    strava = [m for m in members if m.source in STRAVA_SOURCES]
    title = next((m.title for m in strava if m.title), None) or next(
        (m.title for m in members if m.title), None
    )
    description = next((m.description for m in strava if m.description), None) or next(
        (m.description for m in members if m.description), None
    )
    strava_sport = next((m.sport for m in strava if m.sport and m.sport.sport != "other"), None)
    other_sport = next((m.sport for m in members if m.sport and m.sport.sport != "other"), None)
    sport = strava_sport or other_sport
    if (
        strava_sport
        and other_sport
        and strava_sport.sport == other_sport.sport
        and strava_sport.sport_type is None
        and other_sport.sport_type is not None
    ):
        sport = SportInfo(
            strava_sport.sport, other_sport.sport_type, strava_sport.family, strava_sport.raw
        )
    aliases = tuple((m.source, m.source_id) for m in members if m.source_id)
    return CanonicalActivity(
        primary=primary.key,
        curve_sources=curve_sources,
        title=title,
        description=description,
        sport=sport,
        aliases=aliases,
        member_keys=tuple(m.key for m in members),
    )
