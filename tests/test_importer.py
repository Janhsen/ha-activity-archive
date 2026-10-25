"""Plan, ledger and importer with a fake writer (marker: SYNTHETIC)."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta, timezone

from custom_components.activity_archive.adapters.base import SourceActivity
from custom_components.activity_archive.core import importer, loader
from custom_components.activity_archive.core import plan as planning
from custom_components.activity_archive.core.dedupe import GARMIN_FIT, STRAVA_FILE
from custom_components.activity_archive.core.ledger import (
    STATUS_COMPLETE,
    STATUS_WRITING,
    Ledger,
    MemoryLedgerStore,
    RunInProgressError,
)
from custom_components.activity_archive.core.model import StatePoint
from custom_components.activity_archive.core.writer import write_throttled
from tests.fakes import FakeWriter
from tests.fixtures import generator as g

PERSON = "alex"


def fit_item(
    sport: int, sub: int, n: int = 400, *, source_id: str = "1", **kw
) -> importer.ImportItem:
    samples = g.make_track(
        n, gps=kw.pop("gps", True), power=kw.pop("power", False), cadence=kw.pop("cadence", False),
        wander=True, seed=3, **kw,
    )  # fmt: skip
    session = g.session_for(samples, sport, sub, ascent=321, descent=300, avg_hr=131, max_hr=150)
    data = g.encode_fit(samples, session=session, local_offset_s=7200, serial=99)
    return SourceActivity(GARMIN_FIT, source_id, loader.load_bytes(data)).build_item(person=PERSON)


async def fresh() -> tuple[Ledger, FakeWriter]:
    return await Ledger.load(MemoryLedgerStore()), FakeWriter()


class PlanTest(unittest.TestCase):
    def test_states_carry_unit_and_activity_id_only(self) -> None:
        item = fit_item(2, 8, power=True, cadence=True)
        states = item.plan.entity_states()
        hr = states["sensor.activity_archive_alex_heart_rate"]
        self.assertEqual(
            dict(hr[0].attributes),
            {"unit_of_measurement": "bpm", "activity_id": item.plan.activity_id},
        )
        for points in states.values():
            for point in points:
                keys = {k for k, _ in point.attributes}
                self.assertFalse(keys & {"state_class", "device_class"})

    def test_activity_sensor_uses_start_time_and_session_totals(self) -> None:
        item = fit_item(2, 8)
        plan = item.plan
        sensor = plan.entity_states()["sensor.activity_archive_alex"]
        self.assertEqual(len(sensor), 1)
        self.assertEqual(sensor[0].ts, int(g.T0))
        attrs = plan.attributes
        self.assertEqual(attrs["elevation_gain_m"], 321)  # the device's value, not from the curve
        self.assertEqual(attrs["elevation_loss_m"], 300)
        self.assertEqual(attrs["avg_hr_bpm"], 131)
        self.assertEqual(attrs["sport"], "cycling")
        self.assertEqual(attrs["sport_type"], "mountain")
        self.assertEqual(attrs["garmin_id"], "1")
        self.assertEqual(attrs["start_time"], "2000-01-01T10:00:00+00:00")
        self.assertEqual(attrs["start_local"], "2000-01-01T12:00:00")
        self.assertEqual(attrs["tz_source"], "fit")
        self.assertEqual(attrs["schema_version"], 1)

    def test_no_session_means_no_invented_totals(self) -> None:
        raw = loader.load_bytes(g.encode_gpx(g.make_track(300)))
        plan = SourceActivity("strava_file", "7", raw).build_item(person=PERSON).plan
        for name in (
            "distance_m",
            "elevation_gain_m",
            "avg_speed_mps",
            "avg_hr_bpm",
            "moving_time_s",
        ):
            self.assertNotIn(name, plan.attributes)
        self.assertEqual(plan.attributes["tz_source"], "unknown")
        self.assertNotIn("start_local", plan.attributes)
        entities = set(plan.entity_states())
        self.assertNotIn("sensor.activity_archive_alex_speed", entities)
        self.assertNotIn("sensor.activity_archive_alex_distance", entities)
        self.assertNotIn("sensor.activity_archive_alex_power", entities)
        self.assertIn("sensor.activity_archive_alex_heart_rate", entities)
        self.assertIn("device_tracker.activity_archive_alex_position", entities)

    def test_sample_activities_per_sport(self) -> None:
        cases = {
            "mtb": fit_item(2, 8, power=True, cadence=True),
            "trainer": fit_item(2, 6, gps=False, power=True, cadence=True),
            "run": fit_item(1, 0, cadence=True),
            "swim": fit_item(5, 17, gps=False),
        }
        self.assertTrue(
            cases["mtb"].plan.has_gps if hasattr(cases["mtb"].plan, "has_gps") else True
        )
        trainer = cases["trainer"].plan
        self.assertIsNone(trainer.gps)
        self.assertTrue(trainer.attributes["trainer"])
        self.assertNotIn("device_tracker.activity_archive_alex_position", trainer.entity_states())
        self.assertEqual(cases["swim"].plan.sport.sport, "swimming")

    def test_running_cadence_is_steps_per_minute(self) -> None:
        run = fit_item(1, 0, cadence=True).plan
        cadence = next(c for c in run.curves if c.key == "cadence")
        self.assertEqual(cadence.unit, "spm")
        self.assertGreater(min(v for _, v in cadence.points), 150)  # about 2 x 85
        bike = next(c for c in fit_item(2, 8, cadence=True).plan.curves if c.key == "cadence")
        self.assertEqual(bike.unit, "rpm")
        self.assertLess(max(v for _, v in bike.points), 100)

    def test_markers_at_end_and_after_gaps(self) -> None:
        samples = g.make_track(900, wander=True, seed=2)
        for s in samples[300:420]:
            s["lat"] = s["lon"] = None
            s["hr"] = None
        data = g.encode_fit(samples, session=g.session_for(samples, 2, 8))
        plan = (
            SourceActivity(GARMIN_FIT, "2", loader.load_bytes(data)).build_item(person=PERSON).plan
        )
        states = plan.entity_states()
        for entity in (
            "sensor.activity_archive_alex_heart_rate",
            "device_tracker.activity_archive_alex_position",
        ):
            marked = [s.ts for s in states[entity] if s.state == "unavailable"]
            self.assertEqual(len(marked), 2, entity)
            self.assertEqual(marked[0], int(samples[299]["t"]) + 1)
            self.assertEqual(marked[-1], int(samples[899]["t"]) + 1)
        tracker = states["device_tracker.activity_archive_alex_position"]
        self.assertEqual(tracker[0].state, "not_home")
        self.assertIn("latitude", dict(tracker[0].attributes))

    def test_title_and_state_length(self) -> None:
        raw = loader.load_bytes(g.encode_gpx(g.make_track(200), name="x" * 400))
        plan = SourceActivity("strava_file", "7", raw).build_item(person=PERSON).plan
        state = plan.entity_states()["sensor.activity_archive_alex"][0].state
        self.assertEqual(len(state), 255)
        untitled = loader.load_bytes(g.encode_gpx(g.make_track(200), name=""))
        plan = SourceActivity("strava_file", "8", untitled).build_item(person=PERSON).plan
        self.assertEqual(plan.title, "other 2000-01-01")

    def test_attribute_budget(self) -> None:
        big = {
            "schema_version": 1,
            "activity_id": "1",
            "sport_raw": "y" * 20_000,
            "sport": "cycling",
        }
        trimmed = planning.fit_attributes(big)
        self.assertLessEqual(len(json.dumps(trimmed).encode()), 12 * 1024)
        self.assertEqual(trimmed["activity_id"], "1")
        huge = {f"k{i}": "z" * 500 for i in range(100)}
        self.assertLessEqual(len(json.dumps(planning.fit_attributes(huge)).encode()), 12 * 1024)

    def test_short_activity_is_skipped(self) -> None:
        raw = loader.load_bytes(g.encode_fit(g.make_track(30)))
        with self.assertRaises(planning.SkippedError):
            SourceActivity(GARMIN_FIT, "3", raw).build_item(person=PERSON)
        item = SourceActivity(GARMIN_FIT, "3", raw).build_item(person=PERSON, min_duration_s=10)
        self.assertEqual(item.plan.activity_id, str(int(g.T0)))

    def test_summary_without_file_is_not_planned_in_e1(self) -> None:
        with self.assertRaises(planning.SkippedError):
            SourceActivity("strava_csv", "9", None, start=g.T0).build_item(person=PERSON)

    def test_clock_change_night(self) -> None:
        """02:30 local time exists twice on 25 Oct 2026: two different instants and ids."""
        first_start = datetime(2026, 10, 25, 0, 30, tzinfo=UTC).timestamp()  # 02:30 CEST
        second_start = first_start + 3600  # 02:30 CET
        plans = []
        for start, offset in ((first_start, 7200), (second_start, 3600)):
            samples = g.make_track(300, start=start)
            data = g.encode_fit(samples, local_offset_s=offset)
            plans.append(SourceActivity(GARMIN_FIT, str(offset), loader.load_bytes(data)))
        a, b = (p.build_item(person=PERSON).plan for p in plans)
        self.assertEqual(a.attributes["start_local"], b.attributes["start_local"])
        self.assertNotEqual(a.activity_id, b.activity_id)
        self.assertEqual(int(b.activity_id) - int(a.activity_id), 3600)
        self.assertEqual(a.attributes["local_date"], "2026-10-25")

    def test_assumed_time_zone_only_when_the_file_has_none(self) -> None:
        raw = loader.load_bytes(g.encode_gpx(g.make_track(200)))
        berlin_summer = timezone(timedelta(hours=2))
        item = SourceActivity("strava_file", "5", raw).build_item(
            person=PERSON, assumed_tz=berlin_summer
        )
        self.assertEqual(item.plan.attributes["tz_source"], "assumed")
        self.assertEqual(item.plan.attributes["utc_offset_s"], 7200)

    def test_time_jump_back_in_file_still_plans(self) -> None:
        samples = g.make_track(400)
        samples[200]["t"] = g.T0 - 5000
        raw = loader.load_bytes(g.encode_fit(samples))
        plan = SourceActivity(GARMIN_FIT, "6", raw).build_item(person=PERSON, min_duration_s=1).plan
        self.assertTrue(any("not monotonic" in w for w in plan.warnings))

    def test_all_points_inside_the_activity_window(self) -> None:
        item = fit_item(2, 8, power=True, cadence=True)
        plan = item.plan
        for entity, points in plan.entity_states().items():
            for point in points:
                self.assertGreaterEqual(point.ts, plan.start, entity)
                self.assertLessEqual(point.ts, plan.end + 1, entity)


class LedgerTest(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_and_version_gate(self) -> None:
        store = MemoryLedgerStore()
        ledger = await Ledger.load(store)
        run = ledger.begin_run(1000.0, "r1")
        ledger.start_entry(
            "k", run_id=run, source="s", source_id="1", content_hash="h", activity_id="a",
            start=1, end=2, entities=["e"],
        )  # fmt: skip
        ledger.complete_entry("k", {"e": 3})
        ledger.end_run(run, 1001.0)
        await ledger.save()
        again = await Ledger.load(store)
        self.assertEqual(again.status("k"), STATUS_COMPLETE)
        self.assertEqual(again.find("s", "1", None), "k")
        self.assertEqual(again.find("x", None, "h"), "k")
        self.assertEqual(again.run_manifest("r1")[0]["entities"], ["e"])
        wrong = Ledger(store, {"v": 99, "entries": {"k": {}}})
        self.assertIsNone(wrong.get("k"))

    async def test_second_run_is_refused_unless_stale(self) -> None:
        ledger = await Ledger.load(MemoryLedgerStore())
        ledger.begin_run(1000.0, "r1")
        with self.assertRaises(RunInProgressError):
            ledger.begin_run(1500.0)
        ledger.heartbeat("r1", 1400.0)
        with self.assertRaises(RunInProgressError):
            ledger.begin_run(1500.0)
        self.assertEqual(ledger.begin_run(1400.0 + 7 * 3600, "r2"), "r2")  # r1 went stale
        ledger.end_run("r2", 1.0)
        self.assertIsNone(ledger.active_run(2.0))

    async def test_tombstone_survives_reload(self) -> None:
        store = MemoryLedgerStore()
        ledger = await Ledger.load(store)
        ledger.tombstone("strava_file:5")
        await ledger.save()
        self.assertEqual((await Ledger.load(store)).status("strava_file:5"), "tombstone")


class ImporterTest(unittest.IsolatedAsyncioTestCase):
    async def run_import(self, item, ledger, writer, **kw):
        return await importer.import_batch(
            [item], ledger=ledger, writer=writer, clock=lambda: 1000.0, sleep=writer.sleep, **kw
        )

    async def test_second_run_writes_nothing(self) -> None:
        ledger, writer = await fresh()
        items = [fit_item(2, 8, source_id="a"), fit_item(1, 0, source_id="b", cadence=True)]
        first = await importer.import_batch(items, ledger=ledger, writer=writer, clock=lambda: 1.0)
        self.assertEqual(first.count(importer.WRITTEN), 2)
        self.assertGreater(first.new_states, 100)
        before = writer.snapshot()
        second = await importer.import_batch(items, ledger=ledger, writer=writer, clock=lambda: 2.0)
        self.assertEqual(second.new_states, 0)
        self.assertEqual(second.count(importer.SKIPPED_KNOWN), 2)
        self.assertEqual(writer.snapshot(), before)

    async def test_lost_ledger_still_writes_nothing_twice(self) -> None:
        """The database is the truth: a new, empty ledger finds every state already there."""
        ledger, writer = await fresh()
        item = fit_item(2, 8)
        await self.run_import(item, ledger, writer)
        before = writer.snapshot()
        ledger2 = await Ledger.load(MemoryLedgerStore())
        report = await self.run_import(item, ledger2, writer)
        self.assertEqual(report.new_states, 0)
        self.assertEqual(report.count(importer.WRITTEN), 1)
        self.assertEqual(writer.snapshot(), before)

    async def test_abort_at_every_point_gives_the_same_end_state(self) -> None:
        item = fit_item(2, 8, n=150, power=True, cadence=True)
        ledger, reference = await fresh()
        await self.run_import(item, ledger, reference)
        expected = reference.snapshot()
        total = reference.total()
        self.assertGreater(total, 60)
        for cut in range(0, total + 1):
            ledger, writer = await fresh()
            writer.fail_after = cut
            report = await self.run_import(item, ledger, writer)
            if cut < total:
                self.assertEqual(report.outcomes[0].status, importer.FAILED, cut)
                self.assertEqual(ledger.status(item.key), STATUS_WRITING, cut)
            writer.fail_after = None
            again = await self.run_import(item, ledger, writer)
            self.assertEqual(writer.snapshot(), expected, f"cut at {cut}")
            self.assertEqual(ledger.status(item.key), STATUS_COMPLETE)
            self.assertEqual(report.new_states + again.new_states, total)

    async def test_activity_sensor_is_written_last(self) -> None:
        ledger, writer = await fresh()
        item = fit_item(2, 8)
        await self.run_import(item, ledger, writer)
        order = [entity for entity, _ in writer.write_calls]
        self.assertEqual(order[-1], "sensor.activity_archive_alex")

    async def test_unrecorded_entity_blocks_everything(self) -> None:
        ledger, writer = await fresh()
        writer.unrecorded = {"sensor.activity_archive_alex_altitude"}
        report = await self.run_import(fit_item(2, 8), ledger, writer)
        self.assertEqual(report.outcomes[0].status, importer.BLOCKED)
        self.assertEqual(writer.total(), 0)
        self.assertEqual(ledger.data["entries"], {})

    async def test_batch_stops_at_first_failure(self) -> None:
        ledger, writer = await fresh()
        writer.fail_after = 10
        items = [fit_item(2, 8, source_id="a"), fit_item(2, 8, source_id="b")]
        report = await importer.import_batch(items, ledger=ledger, writer=writer)
        self.assertEqual(len(report.outcomes), 1)
        self.assertEqual(ledger.data["runs"][report.run_id]["status"], "stopped")
        self.assertIsNone(ledger.data["active_run"])

    async def test_second_start_is_refused_while_a_run_is_active(self) -> None:
        ledger, writer = await fresh()
        ledger.begin_run(1000.0, "other")
        with self.assertRaises(RunInProgressError):
            await importer.import_batch([], ledger=ledger, writer=writer, clock=lambda: 1001.0)

    async def test_dry_run_writes_and_records_nothing(self) -> None:
        ledger, writer = await fresh()
        report = await self.run_import(fit_item(2, 8), ledger, writer, dry_run=True)
        self.assertEqual(report.outcomes[0].status, importer.DRY_RUN)
        self.assertGreater(sum(report.outcomes[0].planned.values()), 100)
        self.assertEqual(writer.total(), 0)
        self.assertEqual(ledger.data["entries"], {})

    async def test_tombstoned_and_same_content_are_not_imported(self) -> None:
        ledger, writer = await fresh()
        item = fit_item(2, 8, source_id="a")
        ledger.tombstone(item.key)
        report = await self.run_import(item, ledger, writer)
        self.assertEqual(report.outcomes[0].status, importer.SKIPPED_DELETED)
        self.assertEqual(writer.total(), 0)

        ledger, writer = await fresh()
        first = fit_item(2, 8, source_id="a")
        await self.run_import(first, ledger, writer)
        twin = importer.ImportItem(first.plan, GARMIN_FIT, "a", first.content_hash)
        again = importer.ImportItem(first.plan, STRAVA_FILE, "zz", first.content_hash)
        self.assertEqual((await self.run_import(twin, ledger, writer)).new_states, 0)
        self.assertEqual(again.key, "strava_file:zz")

    async def test_manifest_lists_what_a_run_wrote(self) -> None:
        ledger, writer = await fresh()
        item = fit_item(2, 8)
        report = await self.run_import(item, ledger, writer)
        manifest = ledger.run_manifest(report.run_id)
        self.assertEqual([m["key"] for m in manifest], [item.key])
        self.assertIn("sensor.activity_archive_alex", manifest[0]["entities"])
        self.assertEqual(manifest[0]["start"], item.plan.start)

    async def test_stored_states_match_the_plan(self) -> None:
        ledger, writer = await fresh()
        item = fit_item(2, 8, power=True)
        await self.run_import(item, ledger, writer)
        for entity, states in item.plan.entity_states().items():
            stored = writer.store[entity]
            self.assertEqual(sorted(stored), [s.ts for s in states])
            self.assertEqual(stored[states[0].ts].state, states[0].state)


class ThrottleTest(unittest.IsolatedAsyncioTestCase):
    def points(self, n: int) -> list[StatePoint]:
        return [StatePoint(1000 + i, "1") for i in range(n)]

    async def test_blocks_of_500(self) -> None:
        writer = FakeWriter()
        result = await write_throttled(writer, "sensor.a", self.points(1234), sleep=writer.sleep)
        self.assertEqual(result.done, 1234)
        self.assertEqual([n for _, n in writer.write_calls], [500, 500, 234])

    async def test_waits_while_backlog_is_high_then_continues(self) -> None:
        writer = FakeWriter()
        writer.backlog_value = 4000
        writer.drain_per_poll = 1500
        sleeps = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await writer.sleep(seconds)

        result = await write_throttled(writer, "sensor.a", self.points(1200), sleep=sleep)
        self.assertEqual(result.done, 1200)
        self.assertTrue(any(s == 0.5 for s in sleeps))  # it did wait for the queue to drain
        self.assertLess(writer.backlog_value, 5000)

    async def test_hard_limit_stops(self) -> None:
        writer = FakeWriter()
        writer.backlog_value = 5001
        result = await write_throttled(writer, "sensor.a", self.points(10), sleep=writer.sleep)
        self.assertEqual(result.done, 0)
        self.assertEqual(result.error, "recorder queue is full")

    async def test_queue_that_never_drains_gives_up(self) -> None:
        writer = FakeWriter()
        writer.backlog_value = 4000  # no drain
        result = await write_throttled(
            writer, "sensor.a", self.points(10), sleep=writer.sleep, max_polls=5
        )
        self.assertEqual(result.error, "recorder queue does not drain")


if __name__ == "__main__":
    unittest.main()
