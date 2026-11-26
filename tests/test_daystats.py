"""Tests for the per-day reduction of hourly statistics rows (marker: SYNTHETIC)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.activity_archive.core.daystats import last_per_day, mean_per_day

TZ = ZoneInfo("Europe/Berlin")


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


def hour(day: str, h: int) -> float:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, h, tzinfo=TZ).timestamp()


def test_last_takes_last_usable_row_of_each_day():
    rows = [
        (hour("2026-10-20", 3), 10.0, None),
        (hour("2026-10-20", 22), 12.0, None),
        (hour("2026-10-21", 1), None, 7.0),
        (hour("2026-10-21", 5), None, None),  # no value: must not hide the 7.0 before it
    ]
    assert last_per_day(rows, day_of) == {"2026-10-20": 12.0, "2026-10-21": 7.0}


def test_last_prefers_mean_over_state_within_a_row():
    assert last_per_day([(hour("2026-10-20", 3), 1.5, 9.0)], day_of) == {"2026-10-20": 1.5}


def test_days_without_a_value_are_absent():
    assert last_per_day([(hour("2026-10-20", 3), None, None)], day_of) == {}
    assert mean_per_day([(hour("2026-10-20", 3), None, 4.0)], day_of) == {}


def test_mean_averages_hourly_means_and_skips_missing():
    rows = [
        (hour("2026-10-20", 1), 10.0, None),
        (hour("2026-10-20", 2), None, None),
        (hour("2026-10-20", 3), 20.0, None),
    ]
    assert mean_per_day(rows, day_of) == {"2026-10-20": 15.0}


def test_local_day_boundary_follows_the_time_zone_across_dst():
    # 25.10.2026 has 25 hours (end of summer time): 23:00 local still belongs to that day.
    rows = [(hour("2026-10-25", 23), 5.0, None), (hour("2026-10-26", 0), 6.0, None)]
    assert last_per_day(rows, day_of) == {"2026-10-25": 5.0, "2026-10-26": 6.0}
