"""Recorder writer, entity registry and label against the fakes (marker: SYNTHETIC)."""

from __future__ import annotations

import unittest

from custom_components.activity_archive.const import LABEL_LONGTERM, TEST_ENTITY_ID
from custom_components.activity_archive.core.model import StatePoint
from custom_components.activity_archive.core.writer import WriteTargetError, write_throttled
from custom_components.activity_archive.entities import build_catalog
from custom_components.activity_archive.recorder_writer import RecorderStateWriter
from tests.ha_env import PEOPLE, Env

ENTITY = "sensor.activity_archive_alex_heart_rate"


def points(n: int, start: int = 1_700_000_000):
    return [
        StatePoint(start + i * 5, str(100 + i), (("unit_of_measurement", "bpm"),)) for i in range(n)
    ]


class WriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.rec = self.env.recorder
        self.writer = RecorderStateWriter(self.env.hass, self.env.entry, build_catalog(PEOPLE))

    def tearDown(self):
        self.env.close()

    async def test_unknown_entity_is_refused(self):
        with self.assertRaises(WriteTargetError) as ctx:
            await self.writer.check_target("sensor.living_room_temperature")
        self.assertEqual(ctx.exception.key, "unknown_entity")
        self.assertEqual(self.env.hass.entity_registry.entities, {})

    async def test_check_registers_entity_with_unique_id_and_label(self):
        await self.writer.check_target(ENTITY)
        reg = self.env.hass.entity_registry.entities[ENTITY]
        self.assertEqual(reg.unique_id, "activity_archive_alex_heart_rate")
        self.assertEqual(reg.platform, "activity_archive")
        self.assertIn(LABEL_LONGTERM, reg.labels)
        self.assertIn(LABEL_LONGTERM, self.env.hass.label_registry.labels)
        await self.writer.check_target(ENTITY)  # idempotent
        self.assertEqual(len(self.env.hass.entity_registry.entities), 1)
        self.assertEqual(len(self.env.hass.label_registry.labels), 1)

    async def test_existing_label_is_reused_not_duplicated(self):
        existing = self.env.hass.label_registry.async_create(LABEL_LONGTERM)
        await self.writer.check_target(ENTITY)
        self.assertEqual(len(self.env.hass.label_registry.labels), 1)
        self.assertEqual(self.env.hass.entity_registry.entities[ENTITY].labels, {existing.label_id})

    async def test_label_with_other_id_is_found_by_name(self):
        reg = self.env.hass.label_registry
        reg.labels["abc123"] = type(reg.async_create("x"))("abc123", LABEL_LONGTERM)
        del reg.labels["x"]
        await self.writer.check_target(ENTITY)
        self.assertEqual(self.env.hass.entity_registry.entities[ENTITY].labels, {"abc123"})

    async def test_foreign_entity_with_wanted_id_is_not_taken_over(self):
        self.env.hass.entity_registry.foreign_ids.add(ENTITY)
        with self.assertRaises(WriteTargetError) as ctx:
            await self.writer.check_target(ENTITY)
        self.assertEqual(ctx.exception.key, "entity_id_taken")

    async def test_disabled_entity(self):
        await self.writer.check_target(ENTITY)
        self.env.hass.entity_registry.entities[ENTITY].disabled_by = "user"
        with self.assertRaises(WriteTargetError) as ctx:
            await self.writer.check_target(ENTITY)
        self.assertEqual(ctx.exception.key, "entity_disabled")

    async def test_entity_filter(self):
        self.rec.entity_filter = lambda e: not e.endswith("heart_rate")
        with self.assertRaises(WriteTargetError) as ctx:
            await self.writer.check_target(ENTITY)
        self.assertEqual(ctx.exception.key, "not_recorded")
        await self.writer.check_target("sensor.activity_archive_alex_speed")

    async def test_retention(self):
        self.rec.keep_days = 10
        with self.assertRaises(WriteTargetError) as ctx:
            await self.writer.check_target(ENTITY)
        self.assertEqual(ctx.exception.key, "retention_too_short")
        self.rec.auto_purge = False
        await self.writer.check_target(ENTITY)  # no auto purge: keep_days does not matter
        self.rec.auto_purge, self.rec.keep_days = True, 7300
        await self.writer.check_target(ENTITY)  # exactly the minimum

    async def test_test_writer_ignores_retention(self):
        self.rec.keep_days = 10
        test_writer = RecorderStateWriter(
            self.env.hass, self.env.entry, build_catalog(PEOPLE), enforce_retention=False
        )
        await test_writer.check_target(TEST_ENTITY_ID)

    async def test_recorder_not_recording(self):
        self.rec.recording = False
        with self.assertRaises(WriteTargetError) as ctx:
            await self.writer.check_target(ENTITY)
        self.assertEqual(ctx.exception.key, "recorder_not_recording")

    async def test_events_carry_historic_time_and_chain(self):
        result = await self.writer.write(ENTITY, points(3))
        self.assertEqual((result.done, result.error), (3, None))
        events = self.rec.events_seen
        self.assertEqual([e.event_type for e in events], ["state_changed"] * 3)
        self.assertEqual(
            [e.time_fired_timestamp for e in events], [1_700_000_000, 1_700_000_005, 1_700_000_010]
        )
        new = [e.data["new_state"] for e in events]
        self.assertEqual(
            [s.last_updated.timestamp() for s in new], [1_700_000_000, 1_700_000_005, 1_700_000_010]
        )
        for s in new:
            self.assertEqual(s.last_changed, s.last_updated)
            self.assertEqual(s.last_reported, s.last_updated)
            self.assertEqual(s.attributes, {"unit_of_measurement": "bpm"})
        self.assertIs(events[0].data["old_state"], new[0], "first state is its own old state")
        self.assertIs(events[1].data["old_state"], new[0])
        self.assertIs(events[2].data["old_state"], new[1])
        # chain continues across blocks
        await self.writer.write(ENTITY, points(1, 1_700_000_100))
        self.assertIs(self.rec.events_seen[3].data["old_state"], new[2])

    async def test_nothing_reaches_the_state_machine_or_event_bus(self):
        await self.writer.write(ENTITY, points(2))
        self.assertEqual(self.env.hass.services.calls, [])

    async def test_failed_hand_over_stops_and_reports(self):
        self.rec.fail_queue_after = 2
        result = await self.writer.write(ENTITY, points(5))
        self.assertEqual(result.done, 2)
        self.assertIn("RuntimeError", result.error)
        self.assertEqual(self.rec.queued_total, 2)

    async def test_existing_timestamps_waits_for_commit_and_uses_window(self):
        await self.writer.write(ENTITY, points(4))
        self.assertEqual(self.rec.db, {}, "not committed yet")
        found = await self.writer.existing_timestamps(ENTITY, 1_700_000_005, 1_700_000_010)
        self.assertEqual(found, {1_700_000_005, 1_700_000_010})
        self.assertEqual(await self.writer.existing_timestamps(ENTITY, 0, 100), set())

    async def test_backlog(self):
        self.assertEqual(self.writer.backlog, 0)
        await self.writer.write(ENTITY, points(7))
        self.assertEqual(self.writer.backlog, 7)
        self.rec.fixed_backlog = 4200
        self.assertEqual(self.writer.backlog, 4200)

    async def test_throttle_pauses_above_limit_and_stops_at_hard_limit(self):
        # queue drains by 1000 per pause; 1200 states in blocks of 500
        async def sleep(_s):
            self.rec.fixed_backlog = max(0, (self.rec.fixed_backlog or 0) - 1000)

        self.rec.fixed_backlog = 4000
        result = await write_throttled(self.writer, ENTITY, points(1200), sleep=sleep)
        self.assertEqual(result.done, 1200)
        self.rec.fixed_backlog = 5001

        async def no_drain(_s):
            return None

        result = await write_throttled(self.writer, ENTITY, points(10), sleep=no_drain)
        self.assertEqual((result.done, result.error), (0, "recorder queue is full"))


if __name__ == "__main__":
    unittest.main()
