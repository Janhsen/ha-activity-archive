"""Which entities the integration owns, and how they get into the entity registry.

The curve and activity entities have no platform code: they only exist as entity registry
entries (with a ``unique_id``) and as recorder history. That keeps live states out of the
curves, which are written with historic time stamps only. Every id starts with
``activity_archive_`` so that one recorder ``entity_globs`` rule covers all of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from .const import DOMAIN, LABEL_LONGTERM, TEST_ENTITY_ID
from .core.people import Person
from .core.plan import ENTITY_SUFFIX, POSITION_KEY, activity_entity, entity_for
from .core.writer import WriteTargetError

CURVE_TITLES = {
    "heart_rate": "heart rate",
    "altitude": "altitude",
    "speed": "speed",
    "distance": "distance",
    "cadence": "cadence",
    "power": "power",
    "temperature": "temperature",
    POSITION_KEY: "position",
}


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """Registry data of one owned entity."""

    entity_id: str
    domain: str
    unique_id: str
    name: str

    @property
    def object_id(self) -> str:
        """Part of the entity id after the domain."""
        return self.entity_id.split(".", 1)[1]


def _spec(entity_id: str, name: str) -> EntitySpec:
    domain, object_id = entity_id.split(".", 1)
    return EntitySpec(entity_id, domain, object_id, name)


def build_catalog(people: Iterable[Person]) -> dict[str, EntitySpec]:
    """Every entity the integration may write to, keyed by entity id (an allow-list)."""
    catalog = {TEST_ENTITY_ID: _spec(TEST_ENTITY_ID, "Activity Archive test")}
    for person in people:
        activity = activity_entity(person.slug)
        catalog[activity] = _spec(activity, f"{person.name} activity")
        for key in (*ENTITY_SUFFIX, POSITION_KEY):
            entity_id = entity_for(person.slug, key)
            catalog[entity_id] = _spec(entity_id, f"{person.name} activity {CURVE_TITLES[key]}")
    return catalog


def all_entity_ids(people: Iterable[Person]) -> list[str]:
    """Entity ids of all people, without the test entity."""
    return [e for e in build_catalog(people) if e != TEST_ENTITY_ID]


def ensure_label(hass: HomeAssistant) -> str:
    """Return the id of the long-term label, creating the label if it does not exist."""
    registry = lr.async_get(hass)
    if registry.async_get_label(LABEL_LONGTERM) is not None:
        return LABEL_LONGTERM
    existing = registry.async_get_label_by_name(LABEL_LONGTERM)
    if existing is not None:
        return str(existing.label_id)
    return str(registry.async_create(LABEL_LONGTERM).label_id)


def ensure_entity(
    hass: HomeAssistant, entry: Any, spec: EntitySpec, *, label: bool = True
) -> er.RegistryEntry:
    """Register the entity (idempotent), add the long-term label and return the entry.

    Raises :class:`WriteTargetError` if Home Assistant had to pick another id because the wanted
    one belongs to something else.
    """
    registry = er.async_get(hass)
    reg_entry = registry.async_get_or_create(
        spec.domain,
        DOMAIN,
        spec.unique_id,
        suggested_object_id=spec.object_id,
        config_entry=entry,
        original_name=spec.name,
    )
    if reg_entry.entity_id != spec.entity_id:
        raise WriteTargetError("entity_id_taken", spec.entity_id)
    if label:
        label_id = ensure_label(hass)
        if label_id not in reg_entry.labels:
            reg_entry = registry.async_update_entity(
                spec.entity_id, labels=set(reg_entry.labels) | {label_id}
            )
    return reg_entry
