"""Writes states with historic time stamps into the recorder (implements ``StateWriter``).

Home Assistant offers no public way to store a state with a past ``last_updated``: the state
machine always stamps "now" and tells every listener. The only path that reaches the recorder
alone is a hand-built ``state_changed`` event on the recorder's own queue
(``Recorder.queue_task``), the same technique the ``garmin_personal`` integration uses for its
history sensors. It is undocumented and may change with a Home Assistant release, so this
module fails closed: every precondition is checked first, a failed hand-over stops at once and
is reported, and nothing falls back to "now" stamps.

Nothing here talks to the event bus, so no automation, logbook or websocket client sees these
events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.components import recorder
from homeassistant.components.recorder import history
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import MIN_PURGE_KEEP_DAYS
from .core.model import StatePoint
from .core.writer import StateWriter, WriteResult, WriteTargetError
from .entities import EntitySpec, ensure_entity

_LOGGER = logging.getLogger(__name__)


class RecorderStateWriter(StateWriter):
    """Hand states over to the recorder queue, one entity after the other."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        catalog: Mapping[str, EntitySpec],
        *,
        enforce_retention: bool = True,
    ) -> None:
        """Bind to a config entry and the allow-list of writable entities.

        ``enforce_retention=False`` is for the E0 test state only: its purpose is to find out
        whether a state that old survives the purges of this installation.
        """
        self._hass = hass
        self._entry = entry
        self._catalog = catalog
        self._enforce_retention = enforce_retention
        self._previous: dict[str, State] = {}

    def _instance(self) -> Any:
        try:
            return recorder.get_instance(self._hass)
        except Exception as err:  # recorder not set up or API changed
            raise WriteTargetError("recorder_unavailable") from err

    @property
    def backlog(self) -> int:
        """Entries waiting in the recorder queue (0 if the recorder cannot tell)."""
        try:
            return int(getattr(self._instance(), "backlog", 0) or 0)
        except WriteTargetError:
            return 0

    async def check_target(self, entity_id: str) -> None:
        """Raise :class:`WriteTargetError` unless the entity can take and keep states."""
        spec = self._catalog.get(entity_id)
        if spec is None:
            raise WriteTargetError("unknown_entity", entity_id)
        instance = self._instance()
        if not getattr(instance, "recording", True):
            raise WriteTargetError("recorder_not_recording", entity_id)
        reg_entry = ensure_entity(self._hass, self._entry, spec)
        if reg_entry.disabled_by is not None:
            raise WriteTargetError("entity_disabled", entity_id)
        entity_filter = getattr(instance, "entity_filter", None)
        if entity_filter is not None and not entity_filter(entity_id):
            raise WriteTargetError("not_recorded", entity_id)
        if self._enforce_retention and getattr(instance, "auto_purge", True):
            keep_days = getattr(instance, "keep_days", None)
            if isinstance(keep_days, int) and keep_days < MIN_PURGE_KEEP_DAYS:
                raise WriteTargetError("retention_too_short", entity_id)

    async def existing_timestamps(self, entity_id: str, start_ts: int, end_ts: int) -> set[int]:
        """Return the stored time stamps in ``start_ts..end_ts`` after the queue was committed."""
        instance = self._instance()
        await instance.async_block_till_done()
        start = dt_util.utc_from_timestamp(start_ts)
        end = dt_util.utc_from_timestamp(end_ts + 1)
        try:
            states = await instance.async_add_executor_job(
                lambda: history.state_changes_during_period(
                    self._hass,
                    start,
                    end,
                    entity_id,
                    no_attributes=True,
                    include_start_time_state=False,
                )
            )
        except Exception as err:  # the history API differs between versions
            raise WriteTargetError("recorder_unreadable", entity_id) from err
        return {round(s.last_updated.timestamp()) for s in states.get(entity_id, [])}

    async def write(self, entity_id: str, states: Sequence[StatePoint]) -> WriteResult:
        """Queue ``states`` (ascending) as ``state_changed`` events stamped with their own time."""
        done = 0
        try:
            instance = self._instance()
            previous = self._previous.get(entity_id)
            for point in states:
                when = dt_util.utc_from_timestamp(point.ts)
                state = State(
                    entity_id,
                    point.state,
                    point.attrs(),
                    last_changed=when,
                    last_reported=when,
                    last_updated=when,
                )
                event = Event(
                    EVENT_STATE_CHANGED,
                    {
                        "entity_id": entity_id,
                        "old_state": state if previous is None else previous,
                        "new_state": state,
                    },
                    time_fired_timestamp=float(point.ts),
                )
                instance.queue_task(event)
                previous = state
                done += 1
            if previous is not None:
                self._previous[entity_id] = previous
        except asyncio.CancelledError:
            raise
        except Exception as err:  # stop at once; the caller reports what went through
            _LOGGER.error("Handing states to the recorder failed: %s", type(err).__name__)
            return WriteResult(done, f"hand-over failed: {type(err).__name__}")
        return WriteResult(done)
