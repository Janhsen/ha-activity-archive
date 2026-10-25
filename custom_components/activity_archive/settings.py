"""Settings of the config entry as plain objects (no Home Assistant import)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_IMPORT_DIR,
    CONF_PEOPLE,
    CONF_PERSON_ATHLETE,
    CONF_PERSON_NAME,
    CONF_PERSON_SLUG,
    CONF_PROFILE,
    DEFAULT_IMPORT_DIR,
    DEFAULT_PROFILE,
    PROFILES,
)
from .core.people import Person


@dataclass(frozen=True, slots=True)
class Settings:
    """Import folder, simplification profile and people of one entry."""

    import_dir: str
    profile: str
    people: tuple[Person, ...]

    def person(self, slug: str | None) -> Person | None:
        """Return the person with ``slug``; without a slug the only person (if exactly one)."""
        if slug is None:
            return self.people[0] if len(self.people) == 1 else None
        return next((p for p in self.people if p.slug == slug), None)

    def to_data(self) -> dict[str, Any]:
        """Return the storable form."""
        return {
            CONF_IMPORT_DIR: self.import_dir,
            CONF_PROFILE: self.profile,
            CONF_PEOPLE: [
                {
                    CONF_PERSON_NAME: p.name,
                    CONF_PERSON_SLUG: p.slug,
                    CONF_PERSON_ATHLETE: p.athlete_name,
                }
                for p in self.people
            ],
        }


def parse_settings(data: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> Settings:
    """Merge entry data and options (options win) and drop malformed people."""
    merged: dict[str, Any] = {**data, **(options or {})}
    people: list[Person] = []
    for raw in merged.get(CONF_PEOPLE, ()):
        if not isinstance(raw, Mapping) or not raw.get(CONF_PERSON_SLUG):
            continue
        people.append(
            Person(
                slug=str(raw[CONF_PERSON_SLUG]),
                name=str(raw.get(CONF_PERSON_NAME) or raw[CONF_PERSON_SLUG]),
                athlete_name=str(raw.get(CONF_PERSON_ATHLETE) or ""),
            )
        )
    profile = merged.get(CONF_PROFILE)
    return Settings(
        import_dir=str(merged.get(CONF_IMPORT_DIR) or DEFAULT_IMPORT_DIR),
        profile=profile if profile in PROFILES else DEFAULT_PROFILE,
        people=tuple(people),
    )
