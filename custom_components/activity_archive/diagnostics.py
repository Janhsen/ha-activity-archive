"""Diagnostics download: counts and settings only.

No coordinates, no activity data, no person names or slugs, no file names, no full paths.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import ArchiveConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ArchiveConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the entry."""
    return await entry.runtime_data.diagnostics()
