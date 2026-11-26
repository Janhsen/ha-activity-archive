"""Run the WebSocket commands against the real recorder schema (marker: SYNTHETIC).

The unit tests replace the database readers by fakes. This script needs a Python
environment with Home Assistant installed and runs the real SQL on a temporary
SQLite database:

    python scripts/real_recorder_check.py

It prints ``ok`` lines and exits with 1 on the first difference.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.components.recorder.db_schema import (
    Base,
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
)
from homeassistant.helpers.recorder import DATA_INSTANCE
from homeassistant.util import dt as dt_util
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from custom_components.activity_archive import stats_api

TZ = ZoneInfo("Europe/Berlin")
dt_util.set_default_time_zone(TZ)


def check(name: str, ok: bool, detail: object = "") -> None:
    print("ok" if ok else "FAIL", name, "" if ok else detail)
    if not ok:
        sys.exit(1)


def main() -> None:
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    Base.metadata.create_all(engine)
    make = sessionmaker(engine)

    class Instance:
        def get_session(self):
            return make()

        async def async_add_executor_job(self, func, *args):
            return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    class Hass:
        def __init__(self) -> None:
            self.data = {DATA_INSTANCE: Instance()}

    hass = Hass()
    start = datetime(2026, 10, 24, tzinfo=TZ)  # window crosses the end of summer time
    t0 = start.timestamp()
    with make() as s:
        s.add(StatisticsMeta(id=1, statistic_id="sensor.a", source="recorder", has_sum=False))
        hours = [t0 + 3600 * h for h in range(24 * 3 + 1)]
        s.execute(
            Statistics.__table__.insert(),
            [
                {"metadata_id": 1, "start_ts": t, "created_ts": t, "mean": float(i), "state": None}
                for i, t in enumerate(hours)
            ],
        )
        s.add(StatesMeta(metadata_id=1, entity_id="sensor.b"))
        s.add(StatesMeta(metadata_id=2, entity_id="sensor.c"))
        for attributes_id, attrs in ((1, {"k": 1}), (2, {"k": 2}), (3, {"k": 3})):
            s.add(StateAttributes(attributes_id=attributes_id, shared_attrs=json.dumps(attrs)))
        # sensor.b: set 1 before the window (start state), then 2, 1 again, 3
        for offset, state, attributes_id in (
            (-7200, "5", 1),
            (3600, "7", 2),
            (7200, "unavailable", None),
            (90000, "8", 1),
            (180000, "9.5 x", 3),
        ):
            s.add(
                States(
                    metadata_id=1,
                    state=state,
                    attributes_id=attributes_id,
                    last_updated_ts=t0 + offset,
                )
            )
        s.commit()

    end = start + timedelta(days=3)

    def run() -> None:
        rows = stats_api.read_rows(hass, ["sensor.a"], t0, end.timestamp())
        check(
            "read_rows: one row per hour, sorted",
            len(rows["sensor.a"]) == 73,
            len(rows["sensor.a"]),
        )
        states = stats_api.read_states(hass, ["sensor.b", "sensor.c"], t0, end.timestamp())
        check(
            "read_states: start state first, then the window",
            [x[1] for x in states["sensor.b"]] == ["5", "7", "unavailable", "8", "9.5 x"]
            and states["sensor.b"][0][0] == t0
            and states["sensor.c"] == [],
            states,
        )
        sets = stats_api.read_attribute_sets(hass, ["sensor.b", "sensor.c"], t0, end.timestamp())
        got = [(x["a"]["k"], x["f"] - t0, x["l"] - t0) for x in sets["sensor.b"]]
        check(
            "read_attribute_sets: each set once, first and last time",
            got == [(1, 0.0, 90000.0), (2, 3600.0, 3600.0), (3, 180000.0, 180000.0)]
            and sets["sensor.c"] == [],
            got,
        )

    run()
    print("all ok")


if __name__ == "__main__":
    main()
