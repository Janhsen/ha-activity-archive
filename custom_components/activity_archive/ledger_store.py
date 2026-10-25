"""Ledger persistence in Home Assistant's ``.storage`` (implements ``LedgerStore``)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import LEDGER_STORE_KEY, LEDGER_STORE_VERSION
from .core.ledger import LedgerStore


class HaLedgerStore(LedgerStore):
    """One JSON file ``.storage/activity_archive.ledger``."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the store; nothing is read until the first load."""
        self._store: Store[dict[str, Any]] = Store(hass, LEDGER_STORE_VERSION, LEDGER_STORE_KEY)

    async def async_load(self) -> dict[str, Any] | None:
        """Return the saved snapshot or ``None``."""
        data = await self._store.async_load()
        return data if isinstance(data, dict) else None

    async def async_save(self, data: dict[str, Any]) -> None:
        """Save a complete snapshot."""
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        """Delete the file (the config entry is removed)."""
        await self._store.async_remove()
