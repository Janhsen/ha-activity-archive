"""WebSocket command that returns hourly statistics reduced to one value per day.

``recorder/statistics_during_period`` with ``period=hour`` sends every hourly row of every
sensor as JSON. A card that shows one value per day only needs the reduction done here. The
command reads the statistics tables directly (read only) and aggregates in Python, so it does
not depend on the database dialect. It answers every logged-in user, like the recorder command
it replaces, and only for ``sensor.`` statistics.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.components import recorder, websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.daystats import (
    Row,
    StateRow,
    distinct_last_states,
    last_number_per_day,
    last_per_day,
    mean_per_day,
)

WS_TYPE = f"{DOMAIN}/day_stats"
WS_TYPE_STATES = f"{DOMAIN}/day_states"
MAX_IDS = 200
MAX_SPAN_DAYS = 800

_ID_RE = re.compile(r"^sensor\.[a-z0-9_]+$")
# A plain dict: the decorator reads the command name from it and adds the base message schema.
SCHEMA = {
    vol.Required("type"): WS_TYPE,
    vol.Required("start_time"): cv.string,
    vol.Required("end_time"): cv.string,
    vol.Optional("last", default=[]): [cv.string],
    vol.Optional("mean", default=[]): [cv.string],
}

STATES_SCHEMA = {
    vol.Required("type"): WS_TYPE_STATES,
    vol.Required("start_time"): cv.string,
    vol.Required("end_time"): cv.string,
    vol.Optional("last", default=[]): [cv.string],
    vol.Optional("times", default=[]): [cv.string],
}


def _day_of_factory(tz: Any) -> Callable[[float], str]:
    """Local calendar day (ISO date) of a unix time, cached per start time."""
    cache: dict[float, str] = {}

    def day_of(ts: float) -> str:
        day = cache.get(ts)
        if day is None:
            day = cache[ts] = datetime.fromtimestamp(ts, tz).date().isoformat()
        return day

    return day_of


def read_rows(
    hass: HomeAssistant, ids: list[str], start_ts: float, end_ts: float
) -> dict[str, list[Row]]:
    """Read hourly rows (sorted by start) for the given statistic ids. Runs in the recorder pool."""
    from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import select

    out: dict[str, list[Row]] = {i: [] for i in ids}
    with session_scope(hass=hass, read_only=True) as session:
        meta = {
            row.id: row.statistic_id
            for row in session.execute(
                select(StatisticsMeta.id, StatisticsMeta.statistic_id).where(
                    StatisticsMeta.statistic_id.in_(ids)
                )
            )
        }
        if not meta:
            return out
        query = (
            select(Statistics.metadata_id, Statistics.start_ts, Statistics.mean, Statistics.state)
            .where(
                Statistics.metadata_id.in_(list(meta)),
                Statistics.start_ts >= start_ts,
                Statistics.start_ts < end_ts,
            )
            .order_by(Statistics.metadata_id, Statistics.start_ts)
        )
        for metadata_id, start, mean, state in session.execute(query):
            out[meta[metadata_id]].append((start, mean, state))
    return out


def read_states(
    hass: HomeAssistant, ids: list[str], start_ts: float, end_ts: float
) -> dict[str, list[StateRow]]:
    """Read recorded states per entity, like the history API with the start state included.

    The state that was valid at ``start_ts`` comes first with the time ``start_ts``, then every
    row with ``start_ts < last_updated < end_ts`` in time order. Runs in the recorder pool.
    """
    from homeassistant.components.recorder.db_schema import States, StatesMeta
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import select

    out: dict[str, list[StateRow]] = {i: [] for i in ids}
    with session_scope(hass=hass, read_only=True) as session:
        meta = {
            row.metadata_id: row.entity_id
            for row in session.execute(
                select(StatesMeta.metadata_id, StatesMeta.entity_id).where(
                    StatesMeta.entity_id.in_(ids)
                )
            )
        }
        for metadata_id, entity_id in meta.items():
            first = session.execute(
                select(States.state)
                .where(States.metadata_id == metadata_id, States.last_updated_ts <= start_ts)
                .order_by(States.last_updated_ts.desc())
                .limit(1)
            ).first()
            if first is not None:
                out[entity_id].append((start_ts, first[0]))
        if not meta:
            return out
        query = (
            select(States.metadata_id, States.last_updated_ts, States.state)
            .where(
                States.metadata_id.in_(list(meta)),
                States.last_updated_ts > start_ts,
                States.last_updated_ts < end_ts,
            )
            .order_by(States.metadata_id, States.last_updated_ts)
        )
        for metadata_id, when, state in session.execute(query):
            out[meta[metadata_id]].append((when, state))
    return out


def _check_request(msg: dict[str, Any], connection: Any, id_lists: list[list[str]]) -> Any:
    """Shared checks. Returns ``(start_ts, end_ts, ids)`` or None after sending the error."""
    start = dt_util.parse_datetime(msg["start_time"])
    end = dt_util.parse_datetime(msg["end_time"])
    if start is None or end is None or end <= start:
        connection.send_error(msg["id"], "invalid_format", "Invalid time range")
        return None
    if (end - start).days > MAX_SPAN_DAYS:
        connection.send_error(msg["id"], "invalid_format", "Time range too long")
        return None
    ids = sorted({i for lst in id_lists for i in lst})
    if len(ids) > MAX_IDS or not all(_ID_RE.match(i) for i in ids):
        connection.send_error(msg["id"], "invalid_format", "Invalid statistic ids")
        return None
    return start.astimezone(UTC).timestamp(), end.astimezone(UTC).timestamp(), ids


@websocket_api.websocket_command(STATES_SCHEMA)
@websocket_api.async_response
async def ws_day_states(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer ``{"last": {id: {day: value}}, "times": {id: [state, ...]}}`` from recorded states."""
    checked = _check_request(msg, connection, [msg["last"], msg["times"]])
    if checked is None:
        return
    start_ts, end_ts, ids = checked
    if not ids:
        connection.send_result(msg["id"], {"last": {}, "times": {}})
        return
    rows = await recorder.get_instance(hass).async_add_executor_job(
        read_states, hass, ids, start_ts, end_ts
    )
    day_of = _day_of_factory(dt_util.get_default_time_zone())
    connection.send_result(
        msg["id"],
        {
            "last": {i: last_number_per_day(rows[i], day_of) for i in msg["last"]},
            "times": {i: distinct_last_states(rows[i]) for i in msg["times"]},
        },
    )


@websocket_api.websocket_command(SCHEMA)
@websocket_api.async_response
async def ws_day_stats(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer ``{"last": {id: {day: value}}, "mean": {id: {day: value}}}``."""
    start = dt_util.parse_datetime(msg["start_time"])
    end = dt_util.parse_datetime(msg["end_time"])
    if start is None or end is None or end <= start:
        connection.send_error(msg["id"], "invalid_format", "Invalid time range")
        return
    if (end - start).days > MAX_SPAN_DAYS:
        connection.send_error(msg["id"], "invalid_format", "Time range too long")
        return
    last_ids, mean_ids = msg["last"], msg["mean"]
    ids = sorted(set(last_ids) | set(mean_ids))
    if len(ids) > MAX_IDS or not all(_ID_RE.match(i) for i in ids):
        connection.send_error(msg["id"], "invalid_format", "Invalid statistic ids")
        return
    if not ids:
        connection.send_result(msg["id"], {"last": {}, "mean": {}})
        return
    start_ts, end_ts = start.astimezone(UTC).timestamp(), end.astimezone(UTC).timestamp()
    rows = await recorder.get_instance(hass).async_add_executor_job(
        read_rows, hass, ids, start_ts, end_ts
    )
    day_of = _day_of_factory(dt_util.get_default_time_zone())
    connection.send_result(
        msg["id"],
        {
            "last": {i: last_per_day(rows[i], day_of) for i in last_ids},
            "mean": {i: mean_per_day(rows[i], day_of) for i in mean_ids},
        },
    )


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    """Register the command (once, from ``async_setup``)."""
    websocket_api.async_register_command(hass, ws_day_stats)
    websocket_api.async_register_command(hass, ws_day_states)


__all__ = ["WS_TYPE", "async_register_ws", "read_rows", "ws_day_stats"]
