"""Minimal stand-ins for the parts of Home Assistant the HA layer touches (marker: SYNTHETIC).

Home Assistant itself could not be installed where these tests were written, so the HA-facing
modules are tested against these fakes. They encode what the integration ASSUMES about Home
Assistant's API; they prove the logic of this repository, not the match with the real API. The
real-recorder integration tests (pytest-homeassistant-custom-component) are still to be run.

``install()`` registers the fake modules in ``sys.modules`` unless a real Home Assistant is
importable.
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any


# ----------------------------------------------------------------------------- core objects
class State:
    def __init__(
        self,
        entity_id,
        state,
        attributes=None,
        last_changed=None,
        last_reported=None,
        last_updated=None,
    ):
        self.entity_id, self.state = entity_id, state
        self.attributes = dict(attributes or {})
        self.last_changed, self.last_reported, self.last_updated = (
            last_changed,
            last_reported,
            last_updated,
        )


class Event:
    def __init__(self, event_type, data=None, time_fired_timestamp=None):
        self.event_type, self.data, self.time_fired_timestamp = (
            event_type,
            data,
            time_fired_timestamp,
        )


class SupportsResponse(enum.Enum):
    NONE = "none"
    OPTIONAL = "optional"
    ONLY = "only"


class ServiceValidationError(Exception):
    def __init__(
        self, *args, translation_domain=None, translation_key=None, translation_placeholders=None
    ):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


class ConfigEntryState(enum.Enum):
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


class ConfigEntry:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, data=None, options=None, entry_id="entry1", state=ConfigEntryState.LOADED):
        self.entry_id, self.data, self.options, self.state = (
            entry_id,
            data or {},
            options or {},
            state,
        )
        self.runtime_data = None
        self.unload_callbacks: list[Callable[[], None]] = []
        self.update_listeners: list[Any] = []

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: None


class ConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    unique_id = None

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

    def async_show_form(
        self, *, step_id, data_schema=None, errors=None, description_placeholders=None
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_menu(self, *, step_id, menu_options):
        return {"type": "menu", "step_id": step_id, "menu_options": list(menu_options)}


class OptionsFlow(ConfigFlow):
    config_entry: ConfigEntry
    hass: Any


ConfigFlowResult = dict


# ----------------------------------------------------------------------------- voluptuous
class _Marker:
    def __init__(self, schema, default=None):
        self.schema, self.default = schema, default

    def __hash__(self):
        return hash((type(self).__name__, self.schema))

    def __eq__(self, other):
        return isinstance(other, _Marker) and (type(self), self.schema) == (
            type(other),
            other.schema,
        )


class Required(_Marker):
    pass


class Optional(_Marker):
    pass


class Schema:
    def __init__(self, schema, extra=None):
        self.schema = schema


# ----------------------------------------------------------------------------- recorder fakes
class FakeRecorder:
    """Queue, commit, purge and history in one small object."""

    def __init__(self):
        self.keep_days = 36500
        self.auto_purge = True
        self.entity_filter = None
        self.dialect_name = "postgresql"
        self.recording = True
        self.queue: list[Event] = []
        self.db: dict[str, dict[int, State]] = {}
        self.events_seen: list[Event] = []
        self.fixed_backlog: int | None = None
        self.drain_per_block = 0
        self.fail_queue_after: int | None = None
        self.queued_total = 0

    @property
    def backlog(self) -> int:
        return len(self.queue) if self.fixed_backlog is None else self.fixed_backlog

    def queue_task(self, event):
        if self.fail_queue_after is not None and self.queued_total >= self.fail_queue_after:
            raise RuntimeError("simulated recorder failure")
        self.queued_total += 1
        self.queue.append(event)
        self.events_seen.append(event)

    def commit(self):
        for event in self.queue:
            new = event.data["new_state"]
            self.db.setdefault(event.data["entity_id"], {})[round(new.last_updated.timestamp())] = (
                new
            )
        self.queue.clear()

    async def async_block_till_done(self):
        self.commit()

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def purge_entities(self, entity_ids):
        for entity_id in entity_ids:
            self.db.pop(entity_id, None)


# ----------------------------------------------------------------------------- registries
@dataclass
class RegistryEntry:
    entity_id: str
    unique_id: str
    platform: str
    domain: str
    original_name: str | None = None
    labels: set = field(default_factory=set)
    disabled_by: Any = None
    config_entry_id: str | None = None


class FakeEntityRegistry:
    def __init__(self):
        self.entities: dict[str, RegistryEntry] = {}
        self.foreign_ids: set[str] = set()

    def async_get_or_create(
        self,
        domain,
        platform,
        unique_id,
        *,
        suggested_object_id=None,
        config_entry=None,
        original_name=None,
    ):
        for entry in self.entities.values():
            if (entry.domain, entry.platform, entry.unique_id) == (domain, platform, unique_id):
                return entry
        entity_id = f"{domain}.{suggested_object_id}"
        n = 2
        while entity_id in self.entities or entity_id in self.foreign_ids:
            entity_id = f"{domain}.{suggested_object_id}_{n}"
            n += 1
        entry = RegistryEntry(
            entity_id, unique_id, platform, domain, original_name,
            config_entry_id=getattr(config_entry, "entry_id", None),
        )  # fmt: skip
        self.entities[entity_id] = entry
        return entry

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_update_entity(self, entity_id, *, labels=None, **_kw):
        entry = self.entities[entity_id]
        if labels is not None:
            entry.labels = set(labels)
        return entry

    def async_remove(self, entity_id):
        self.entities.pop(entity_id, None)


@dataclass
class LabelEntry:
    label_id: str
    name: str


class FakeLabelRegistry:
    def __init__(self):
        self.labels: dict[str, LabelEntry] = {}

    def async_get_label(self, label_id):
        return self.labels.get(label_id)

    def async_get_label_by_name(self, name):
        return next((lab for lab in self.labels.values() if lab.name == name), None)

    def async_create(self, name):
        entry = LabelEntry(name.lower().replace(" ", "_"), name)
        self.labels[entry.label_id] = entry
        return entry


class FakeStore:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, version, key):
        self.hass, self.version, self.key = hass, version, key

    async def async_load(self):
        import copy

        return copy.deepcopy(self.hass.storage.get(self.key))

    async def async_save(self, data):
        import copy

        self.hass.storage[self.key] = copy.deepcopy(data)

    async def async_remove(self):
        self.hass.storage.pop(self.key, None)


class IssueSeverity(enum.StrEnum):
    ERROR = "error"
    WARNING = "warning"
    CRITICAL = "critical"


# ----------------------------------------------------------------------------- hass
class FakeConfig:
    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.allowed: list[str] = []

    def path(self, *parts):
        import os

        return os.path.join(self.config_dir, *parts)

    def is_allowed_path(self, path: str) -> bool:
        return any(path == a or path.startswith(a.rstrip("/") + "/") for a in self.allowed)


class FakeServices:
    def __init__(self, hass):
        self.hass = hass
        self.calls: list[tuple[str, str, dict]] = []
        self.registered: dict[tuple[str, str], Any] = {}

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data)))
        if (domain, service) == ("recorder", "purge_entities"):
            assert data["keep_days"] == 0
            self.hass.recorder.commit()
            self.hass.recorder.purge_entities(data["entity_id"])


class FakeConfigEntries:
    def __init__(self):
        self.entries: list[ConfigEntry] = []
        self.reloaded: list[str] = []

    def async_entries(self, domain=None):
        return list(self.entries)

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)


class FakeHass:
    def __init__(self, config_dir: str):
        self.config = FakeConfig(config_dir)
        self.recorder = FakeRecorder()
        self.entity_registry = FakeEntityRegistry()
        self.label_registry = FakeLabelRegistry()
        self.issues: dict[tuple[str, str], dict] = {}
        self.storage: dict[str, Any] = {}
        self.services = FakeServices(self)
        self.config_entries = FakeConfigEntries()
        self.admin_services: dict[str, tuple[Any, Any, Any]] = {}

    async def async_add_executor_job(self, func, *args):
        return func(*args)


# ----------------------------------------------------------------------------- installation
def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


def _state_changes_during_period(
    hass, start, end=None, entity_id=None, no_attributes=False, include_start_time_state=True, **_kw
):
    rows = hass.recorder.db.get(entity_id, {})
    lo, hi = start.timestamp(), end.timestamp()
    return {entity_id: [s for ts, s in sorted(rows.items()) if lo <= ts < hi]}


def _create_issue(
    hass, domain, issue_id, *, severity, translation_key, translation_placeholders=None, **kw
):
    hass.issues[(domain, issue_id)] = {
        "severity": severity,
        "translation_key": translation_key,
        "placeholders": translation_placeholders or {},
        **kw,
    }


def _delete_issue(hass, domain, issue_id):
    hass.issues.pop((domain, issue_id), None)


def _register_admin_service(hass, domain, service, func, schema=None, *, supports_response=None):
    hass.admin_services[service] = (func, schema, supports_response)


def _selector_cls(name):
    return type(name, (), {"__init__": lambda self, *a, **k: setattr(self, "config", (a, k))})


def install() -> bool:
    """Install the fakes; returns False (and does nothing) if real Home Assistant is present."""
    if "homeassistant.fake" in sys.modules:
        return True
    if importlib.util.find_spec("homeassistant") is not None:
        return False
    _module("homeassistant.fake")
    _module("voluptuous", Schema=Schema, Required=Required, Optional=Optional, PREVENT_EXTRA=0)
    ha = _module("homeassistant")
    ha.__path__ = []
    _module(
        "homeassistant.core",
        HomeAssistant=FakeHass,
        State=State,
        Event=Event,
        ServiceCall=object,
        SupportsResponse=SupportsResponse,
        callback=lambda f: f,
    )
    _module("homeassistant.const", EVENT_STATE_CHANGED="state_changed")
    _module("homeassistant.exceptions", ServiceValidationError=ServiceValidationError)
    _module(
        "homeassistant.config_entries",
        ConfigEntry=ConfigEntry,
        ConfigFlow=ConfigFlow,
        OptionsFlow=OptionsFlow,
        ConfigFlowResult=ConfigFlowResult,
        ConfigEntryState=ConfigEntryState,
    )
    components = _module("homeassistant.components")
    components.__path__ = []
    history = _module(
        "homeassistant.components.recorder.history",
        state_changes_during_period=_state_changes_during_period,
    )
    rec = _module(
        "homeassistant.components.recorder",
        get_instance=lambda hass: hass.recorder,
        DOMAIN="recorder",
        history=history,
    )
    rec.__path__ = []
    components.recorder = rec
    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []
    registries = {
        "entity_registry": {
            "async_get": lambda h: h.entity_registry,
            "RegistryEntry": RegistryEntry,
        },
        "label_registry": {"async_get": lambda h: h.label_registry},
        "issue_registry": {
            "async_create_issue": _create_issue,
            "async_delete_issue": _delete_issue,
            "IssueSeverity": IssueSeverity,
        },
        "config_validation": {
            "string": str,
            "boolean": bool,
            "config_entry_only_config_schema": lambda d: ("schema", d),
        },
    }
    for attr, members in registries.items():
        setattr(helpers, attr, _module(f"homeassistant.helpers.{attr}", **members))
    _module("homeassistant.helpers.storage", Store=FakeStore)
    _module(
        "homeassistant.helpers.selector",
        SelectSelector=_selector_cls("SelectSelector"),
        SelectSelectorConfig=_selector_cls("SelectSelectorConfig"),
        SelectSelectorMode=types.SimpleNamespace(LIST="list"),
        TextSelector=_selector_cls("TextSelector"),
    )
    _module("homeassistant.helpers.start", async_at_started=lambda hass, cb: lambda: None)
    _module("homeassistant.helpers.service", async_register_admin_service=_register_admin_service)
    _module("homeassistant.helpers.typing", ConfigType=dict)
    util = _module("homeassistant.util")
    util.__path__ = []
    dtmod = _module(
        "homeassistant.util.dt",
        utc_from_timestamp=lambda ts: datetime.fromtimestamp(ts, UTC),
        get_default_time_zone=lambda: UTC,
    )
    util.dt = dtmod
    return True


async def drain(hass: FakeHass) -> None:
    """Let the fake recorder commit (used as ``sleep`` replacement)."""
    hass.recorder.commit()
    await asyncio.sleep(0)


__all__ = ["FakeHass", "install", "tzinfo"]
