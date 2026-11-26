"""Test of the day_stats WebSocket handler with a fake connection (marker: SYNTHETIC)."""

import asyncio
from datetime import UTC, datetime

from custom_components.activity_archive import stats_api
from tests import ha_stubs

ha_stubs.install()


class Conn:
    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, _id, data):
        self.result = data

    def send_error(self, _id, code, message):
        self.error = (code, message)


def ts(day: int, hour: int) -> float:
    return datetime(2026, 10, day, hour, tzinfo=UTC).timestamp()


def run(msg, rows, monkeypatch):
    monkeypatch.setattr(stats_api, "read_rows", lambda hass, ids, a, b: rows)
    hass = ha_stubs.FakeHass("/tmp")
    conn = Conn()
    asyncio.run(stats_api.ws_day_stats(hass, conn, msg))
    return conn


BASE = {"id": 1, "type": "activity_archive/day_stats"}


def test_reduces_rows_to_one_value_per_day(monkeypatch):
    rows = {
        "sensor.a": [(ts(20, 3), 1.0, None), (ts(20, 9), 3.0, None), (ts(21, 4), 5.0, None)],
        "sensor.b": [(ts(20, 3), 2.0, None), (ts(20, 9), 4.0, None)],
    }
    msg = {
        **BASE,
        "start_time": "2026-10-19T00:00:00Z",
        "end_time": "2026-10-23T00:00:00Z",
        "last": ["sensor.a"],
        "mean": ["sensor.b"],
    }
    conn = run(msg, rows, monkeypatch)
    assert conn.result == {
        "last": {"sensor.a": {"2026-10-20": 3.0, "2026-10-21": 5.0}},
        "mean": {"sensor.b": {"2026-10-20": 3.0}},
    }


def test_rejects_foreign_ids_and_bad_ranges(monkeypatch):
    bad_id = {**BASE, "start_time": "2026-10-19T00:00:00Z", "end_time": "2026-10-20T00:00:00Z"}
    assert run({**bad_id, "last": ["person.x"], "mean": []}, {}, monkeypatch).error
    reversed_range = {
        **BASE,
        "start_time": "2026-10-20T00:00:00Z",
        "end_time": "2026-10-19T00:00:00Z",
    }
    assert run({**reversed_range, "last": [], "mean": []}, {}, monkeypatch).error


def test_empty_request_answers_empty(monkeypatch):
    msg = {
        **BASE,
        "start_time": "2026-10-19T00:00:00Z",
        "end_time": "2026-10-20T00:00:00Z",
        "last": [],
        "mean": [],
    }
    assert run(msg, {}, monkeypatch).result == {"last": {}, "mean": {}}


def run_states(msg, rows, monkeypatch):
    monkeypatch.setattr(stats_api, "read_states", lambda hass, ids, a, b: rows)
    hass = ha_stubs.FakeHass("/tmp")
    conn = Conn()
    asyncio.run(stats_api.ws_day_states(hass, conn, msg))
    return conn


def test_day_states_reduces_states(monkeypatch):
    rows = {
        "sensor.a": [(ts(20, 3), "1"), (ts(20, 9), "3"), (ts(21, 4), "unknown")],
        "sensor.b": [
            (ts(20, 3), "2026-10-20T04:00:00+00:00"),
            (ts(21, 3), "2026-10-21T04:00:00+00:00"),
        ],
    }
    msg = {
        "id": 1,
        "type": "activity_archive/day_states",
        "start_time": "2026-10-19T00:00:00Z",
        "end_time": "2026-10-23T00:00:00Z",
        "last": ["sensor.a"],
        "times": ["sensor.b"],
    }
    conn = run_states(msg, rows, monkeypatch)
    assert conn.result == {
        "last": {"sensor.a": {"2026-10-20": 3.0}},
        "times": {"sensor.b": ["2026-10-20T04:00:00+00:00", "2026-10-21T04:00:00+00:00"]},
    }


def test_day_states_rejects_foreign_ids(monkeypatch):
    msg = {
        "id": 1,
        "type": "activity_archive/day_states",
        "start_time": "2026-10-19T00:00:00Z",
        "end_time": "2026-10-20T00:00:00Z",
        "last": ["person.x"],
        "times": [],
    }
    assert run_states(msg, {}, monkeypatch).error
