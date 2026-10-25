"""Activity Archive: sport activities as long-term recorder states.

The Home-Assistant-free core lives in ``core`` and the adapter interface in ``adapters``. This
package adds the Home Assistant side: config flow, recorder writer, ledger storage, services,
start checks and diagnostics.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .manager import ArchiveManager
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ArchiveConfigEntry = ConfigEntry[ArchiveManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once for the whole integration."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ArchiveConfigEntry) -> bool:
    """Set up the entry: runtime object, start checks and reload on option changes."""
    manager = ArchiveManager(hass, entry)
    await manager.async_setup()
    entry.runtime_data = manager

    async def _checks(_hass: HomeAssistant) -> None:
        found = await manager.async_run_checks()
        if found:
            _LOGGER.warning("Start checks found: %s (see Settings > Repairs)", ", ".join(found))

    # Recorder filters and retention are settled once Home Assistant has started.
    entry.async_on_unload(async_at_started(hass, _checks))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ArchiveConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ArchiveConfigEntry) -> bool:
    """Unload the entry; written states and the ledger stay."""
    return True
