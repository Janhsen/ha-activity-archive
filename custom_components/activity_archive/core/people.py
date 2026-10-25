"""Person slugs: the part of an entity id that identifies a person.

Entity ids look like ``sensor.activity_archive_<slug>`` (activity) and
``sensor.activity_archive_<slug>_heart_rate`` (curve). A slug must therefore never end in a curve
suffix, otherwise two people could share an entity id.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from .plan import ENTITY_SUFFIX, POSITION_KEY

SLUG_RE: Final = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
MAX_SLUG_LENGTH: Final = 24
RESERVED_WORDS: Final = frozenset({"test", POSITION_KEY, *ENTITY_SUFFIX.values()})

ERR_INVALID: Final = "invalid_slug"
ERR_RESERVED: Final = "slug_reserved"
ERR_EXISTS: Final = "slug_exists"

_TRANSLIT: Final = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"}


def slugify(name: str) -> str:
    """Turn a display name into a slug (ASCII, lower case, underscores)."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in name)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:MAX_SLUG_LENGTH].strip("_")


def validate_slug(slug: str, existing: Iterable[str] = ()) -> str | None:
    """Return an error key or ``None`` if ``slug`` is usable next to the ``existing`` ones."""
    if not slug or len(slug) > MAX_SLUG_LENGTH or not SLUG_RE.match(slug):
        return ERR_INVALID
    if any(slug == word or slug.endswith(f"_{word}") for word in RESERVED_WORDS):
        return ERR_RESERVED
    if slug in set(existing):
        return ERR_EXISTS
    return None


@dataclass(frozen=True, slots=True)
class Person:
    """A person whose activities are archived."""

    slug: str
    name: str
    athlete_name: str = ""
