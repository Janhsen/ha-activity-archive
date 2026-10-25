"""Activity Archive: sport activities as long-term recorder states.

Stage E1 contains only the Home-Assistant-free core in ``core`` and the adapter interface in
``adapters``. The config flow, the recorder writer and the services follow in later stages.
"""

from __future__ import annotations

from typing import Any

from .const import DOMAIN


async def async_setup(hass: Any, config: dict[str, Any]) -> bool:
    """Set up the integration (nothing to do yet in stage E1)."""
    hass.data.setdefault(DOMAIN, {})
    return True
