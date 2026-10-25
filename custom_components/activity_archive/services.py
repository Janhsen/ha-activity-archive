"""Services of the integration (all administrator-only, all answer with a result)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .manager import ArchiveError, ArchiveManager

SERVICE_IMPORT_FILE = "import_file"
SERVICE_VERIFY = "verify"
SERVICE_ROLLBACK = "rollback"
SERVICE_WRITE_TEST_STATE = "write_test_state"
SERVICE_REMOVE_TEST_STATE = "remove_test_state"

ATTR_FILE = "file"
ATTR_PERSON = "person"
ATTR_DRY_RUN = "dry_run"
ATTR_RUN_ID = "run_id"
ATTR_CONFIRM = "confirm"
ATTR_PURGE = "purge_entities"

IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_FILE): cv.string,
        vol.Optional(ATTR_PERSON): cv.string,
        # Writing cannot be undone state by state, so the default is to only read and count.
        vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
    }
)
VERIFY_SCHEMA = vol.Schema({vol.Optional(ATTR_RUN_ID): cv.string})
ROLLBACK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_RUN_ID): cv.string,
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
        vol.Optional(ATTR_PURGE, default=False): cv.boolean,
    }
)
NO_FIELDS_SCHEMA = vol.Schema({})


def _manager(hass: HomeAssistant) -> ArchiveManager:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            manager: ArchiveManager = entry.runtime_data
            return manager
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")


async def _run(
    hass: HomeAssistant, action: Callable[[ArchiveManager], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    try:
        return await action(_manager(hass))
    except ArchiveError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders=err.placeholders,
        ) from err


def async_register_services(hass: HomeAssistant) -> None:
    """Register the services (once, from ``async_setup``)."""

    async def import_file(call: ServiceCall) -> dict[str, Any]:
        data = call.data
        return await _run(
            hass,
            lambda m: m.import_file(data[ATTR_FILE], data.get(ATTR_PERSON), data[ATTR_DRY_RUN]),
        )

    async def verify(call: ServiceCall) -> dict[str, Any]:
        return await _run(hass, lambda m: m.verify(call.data.get(ATTR_RUN_ID)))

    async def rollback(call: ServiceCall) -> dict[str, Any]:
        data = call.data
        return await _run(
            hass, lambda m: m.rollback(data[ATTR_RUN_ID], data[ATTR_CONFIRM], data[ATTR_PURGE])
        )

    async def write_test_state(call: ServiceCall) -> dict[str, Any]:
        return await _run(hass, lambda m: m.write_test_state())

    async def remove_test_state(call: ServiceCall) -> dict[str, Any]:
        return await _run(hass, lambda m: m.remove_test_state())

    for name, handler, schema in (
        (SERVICE_IMPORT_FILE, import_file, IMPORT_SCHEMA),
        (SERVICE_VERIFY, verify, VERIFY_SCHEMA),
        (SERVICE_ROLLBACK, rollback, ROLLBACK_SCHEMA),
        (SERVICE_WRITE_TEST_STATE, write_test_state, NO_FIELDS_SCHEMA),
        (SERVICE_REMOVE_TEST_STATE, remove_test_state, NO_FIELDS_SCHEMA),
    ):
        async_register_admin_service(
            hass, DOMAIN, name, handler, schema, supports_response=SupportsResponse.OPTIONAL
        )
