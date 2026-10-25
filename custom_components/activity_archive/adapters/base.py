"""Interface of source adapters (Garmin FIT, Strava export, optional Strava API).

An adapter yields :class:`SourceActivity` objects in a uniform format; de-duplication, ledger and
writer exist once in the core. Adapters never talk to the recorder and never decide which source
wins. Stage E1 defines the interface only; the adapters follow in E2 and E3.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import tzinfo

from ..const import DEFAULT_PROFILE, MIN_DURATION_S
from ..core import sports
from ..core.dedupe import ActivityFacts
from ..core.importer import ImportItem
from ..core.model import RawActivity
from ..core.plan import SkippedError, available_curves, build_plan, sport_of


@dataclass(slots=True)
class SourceActivity:
    """One activity as an adapter found it.

    ``raw`` holds the parsed samples (``None`` for a summary without a file, such as a row of the
    Strava export CSV). The scalar fields override what the raw data would say and carry
    metadata only the source knows (title, description, sport). ``origin`` is a display name
    without directories, safe for reports.
    """

    kind: str
    source_id: str | None = None
    raw: RawActivity | None = None
    origin: str | None = None
    start: float | None = None
    duration_s: float | None = None
    distance_m: float | None = None
    sport: sports.SportInfo | None = None
    title: str | None = None
    description: str | None = None
    summary: dict[str, float | int | str] = field(default_factory=dict)

    def to_facts(self, key: str) -> ActivityFacts:
        """Condense into what the duplicate comparison needs. Nothing is calculated from samples."""
        raw = self.raw
        start = self.start if self.start is not None else (raw.start if raw else None)
        if start is None:
            raise ValueError("activity without a start time")
        duration = self.duration_s
        distance = self.distance_m
        if raw is not None:
            if duration is None:
                elapsed = raw.session.get("elapsed_s")
                if isinstance(elapsed, int | float):
                    duration = float(elapsed)
                elif raw.t:
                    duration = raw.t[-1] - raw.t[0]
            if distance is None:
                total = raw.session.get("distance_m")
                distance = float(total) if isinstance(total, int | float) else None
        return ActivityFacts(
            key=key,
            source=self.kind,
            start=float(start),
            source_id=self.source_id,
            content_hash=raw.content_hash if raw else None,
            file_id=raw.file_id if raw else None,
            duration_s=duration,
            distance_m=distance,
            sport=self.sport or (sport_of(raw) if raw else None),
            file_format=raw.source_format if raw else None,
            curves=available_curves(raw) if raw else frozenset(),
            title=self.title or (raw.title if raw else None),
            description=self.description,
        )

    def build_item(
        self,
        *,
        person: str,
        profile: str = DEFAULT_PROFILE,
        assumed_tz: tzinfo | None = None,
        min_duration_s: float = MIN_DURATION_S,
    ) -> ImportItem:
        """Plan this activity for import; raises ``SkippedError`` if it cannot or should not be."""
        if self.raw is None:
            raise SkippedError("summary without a file: written once a curve source exists (E2)")
        plan = build_plan(
            self.raw,
            person=person,
            source=self.kind,
            source_id=self.source_id,
            profile=profile,
            title=self.title,
            sport=self.sport,
            min_duration_s=min_duration_s,
            assumed_tz=assumed_tz,
        )
        return ImportItem(plan, self.kind, self.source_id, self.raw.content_hash)


class SourceAdapter(ABC):
    """A source of activities for one person."""

    #: Short name used in the ledger and reports, for example ``garmin_fit``.
    name: str = ""

    def __init__(self, person: str) -> None:
        """Bind the adapter to a person slug; files always belong to this person."""
        self.person = person

    @abstractmethod
    def activities(self) -> AsyncIterator[SourceActivity]:
        """Yield the activities, one at a time (never all parsed files at once)."""

    async def async_close(self) -> None:
        """Release resources (open archives, HTTP sessions)."""
        return None
