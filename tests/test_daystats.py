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


def test_last_number_per_day_reads_leading_number_and_skips_text():
    from custom_components.activity_archive.core.daystats import last_number_per_day

    rows = [(1.0, "5"), (2.0, "unavailable"), (3.0, "7.5 kg"), (90000.0, "x"), (90001.0, "-2")]
    day = lambda t: "d1" if t < 86400 else "d2"  # noqa: E731
    assert last_number_per_day(rows, day) == {"d1": 7.5, "d2": -2.0}


def test_distinct_last_states_keeps_last_occurrence_in_order():
    from custom_components.activity_archive.core.daystats import distinct_last_states

    rows = [(1, "a"), (2, "b"), (3, "a"), (4, "unknown"), (5, None), (6, "c"), (7, "b")]
    assert distinct_last_states(rows) == ["a", "c", "b"]


def test_last_number_per_day_only_ascii_numbers():
    from custom_components.activity_archive.core.daystats import last_number_per_day

    rows = [(1.0, "٣"), (2.0, "\x1c7"), (3.0, " 12"), (4.0, "1e"), (5.0, "Infinity")]
    assert last_number_per_day(rows, lambda t: str(int(t))) == {"3": 12.0, "4": 1.0}
