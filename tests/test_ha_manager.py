"""Import, verify, rollback, test state and diagnostics through the manager (marker: SYNTHETIC)."""

from __future__ import annotations

import asyncio
import json
import os
import unittest

from custom_components.activity_archive.const import (
    LEDGER_STORE_KEY,
    TEST_ENTITY_ID,
    TEST_TIMESTAMP,
)
from custom_components.activity_archive.manager import ArchiveError
from tests.ha_env import Env


class ImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.m = self.env.manager

    def tearDown(self):
        self.env.close()

    async def test_dry_run_counts_and_writes_nothing(self):
        self.env.put_fit()
        result = await self.m.import_file("a.fit", "alex", True)
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["dry_run"])
        self.assertGreater(result["total_states"], 100)
        self.assertEqual(result["total_states"], sum(result["planned"].values()))
        self.assertEqual(self.env.recorder.events_seen, [])
        self.assertEqual(self.env.hass.entity_registry.entities, {}, "dry run registers nothing")
        self.assertNotIn(
            LEDGER_STORE_KEY, self.env.hass.storage, "dry run does not touch the ledger"
        )

    async def test_import_writes_exactly_the_planned_states(self):
        self.env.put_fit()
        dry = await self.m.import_file("a.fit", "alex", True)
        real = await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(real["status"], "written")
        self.assertEqual(real["new_states"], dry["total_states"])
        self.env.recorder.commit()
        stored = {e: len(rows) for e, rows in self.env.recorder.db.items()}
        self.assertEqual(stored, dry["planned"])
        self.assertTrue(
            all(
                e.startswith(
                    ("sensor.activity_archive_alex", "device_tracker.activity_archive_alex")
                )
                for e in stored
            )
        )
        # activity sensor last in the queue, with its own state text
        self.assertEqual(
            self.env.recorder.events_seen[-1].data["entity_id"], "sensor.activity_archive_alex"
        )
        # the ledger is in the store and the run is closed
        ledger = self.env.hass.storage[LEDGER_STORE_KEY]
        self.assertIsNone(ledger["active_run"])
        self.assertEqual(ledger["runs"][real["run_id"]]["status"], "done")
        # entities exist in the registry with the label
        regs = self.env.hass.entity_registry.entities
        self.assertEqual(set(regs), set(stored))
        self.assertTrue(all("recorder_longterm" in r.labels for r in regs.values()))

    async def test_second_run_and_run_after_restart_write_nothing(self):
        self.env.put_fit()
        await self.m.import_file("a.fit", "alex", False)
        queued = self.env.recorder.queued_total
        again = await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(again["status"], "skipped_known")
        m2 = self.env.new_manager()
        after_restart = await m2.import_file("a.fit", "alex", False)
        self.assertEqual(after_restart["status"], "skipped_known")
        self.assertEqual(self.env.recorder.queued_total, queued)

    async def test_interrupted_import_is_resumed_to_the_same_result(self):
        self.env.put_fit()
        clean = Env()
        clean.put_fit()
        await clean.manager.import_file("a.fit", "alex", False)
        clean.recorder.commit()
        expected = {e: set(rows) for e, rows in clean.recorder.db.items()}
        clean.close()

        self.env.recorder.fail_queue_after = 100
        failed = await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(failed["status"], "failed")
        self.env.recorder.commit()
        self.assertLess(
            sum(len(r) for r in self.env.recorder.db.values()), sum(map(len, expected.values()))
        )
        ledger = self.env.hass.storage[LEDGER_STORE_KEY]
        self.assertEqual(ledger["runs"][failed["run_id"]]["status"], "stopped")

        self.env.recorder.fail_queue_after = None
        m2 = self.env.new_manager()  # a restart in between
        resumed = await m2.import_file("a.fit", "alex", False)
        self.assertEqual(resumed["status"], "written")
        self.env.recorder.commit()
        got = {e: set(rows) for e, rows in self.env.recorder.db.items()}
        self.assertEqual(got, expected)
        self.assertEqual(
            self.env.recorder.queued_total,
            sum(map(len, expected.values())),
            "nothing written twice",
        )

    async def test_stale_run_from_a_crashed_process_does_not_block(self):
        self.env.put_fit()
        self.env.hass.storage[LEDGER_STORE_KEY] = {
            "v": 1,
            "entries": {},
            "runs": {
                "r0": {
                    "started": 1.0,
                    "heartbeat": 2e12,
                    "ended": None,
                    "status": "running",
                    "entries": [],
                }
            },
            "active_run": "r0",
        }
        result = await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(result["status"], "written")
        self.assertEqual(self.env.hass.storage[LEDGER_STORE_KEY]["runs"]["r0"]["status"], "aborted")

    async def test_unknown_ledger_version_blocks_and_is_left_alone(self):
        self.env.put_fit()
        future = {"v": 99, "something": "new"}
        self.env.hass.storage[LEDGER_STORE_KEY] = future
        with self.assertRaises(ArchiveError) as ctx:
            await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(ctx.exception.key, "ledger_unreadable")
        self.assertEqual(self.env.hass.storage[LEDGER_STORE_KEY], future)
        self.assertIn(("activity_archive", "ledger_unreadable"), self.env.hass.issues)
        self.assertEqual(self.env.recorder.events_seen, [])

    async def test_blocked_target_is_reported_not_raised(self):
        self.env.put_fit()
        self.env.recorder.keep_days = 10
        result = await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("retention_too_short", result["reason"])
        self.assertEqual(self.env.recorder.events_seen, [])

    async def test_busy_lock(self):
        self.env.put_fit()
        async with self.m.lock:
            with self.assertRaises(ArchiveError) as ctx:
                await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(ctx.exception.key, "run_in_progress")

    async def test_two_parallel_imports_second_is_refused(self):
        self.env.put_fit()
        self.env.put_fit("b.fit", seed=5)
        first = asyncio.create_task(self.m.import_file("a.fit", "alex", False))
        await asyncio.sleep(0)
        with self.assertRaises(ArchiveError) as ctx:
            await self.m.import_file("b.fit", "alex", False)
        self.assertEqual(ctx.exception.key, "run_in_progress")
        self.assertEqual((await first)["status"], "written")

    async def test_short_activity_is_reported_as_skipped(self):
        self.env.put_fit(n=20)
        result = await self.m.import_file("a.fit", "alex", False)
        self.assertEqual(result["status"], "skipped")

    async def test_person_rules(self):
        self.env.put_fit()
        with self.assertRaises(ArchiveError) as ctx:
            await self.m.import_file("a.fit", None, True)
        self.assertEqual(ctx.exception.key, "person_required")
        with self.assertRaises(ArchiveError) as ctx:
            await self.m.import_file("a.fit", "nobody", True)
        self.assertEqual(ctx.exception.key, "person_unknown")
        single = Env(people=self.env.settings.people[:1])
        single.put_fit()
        self.assertEqual((await single.manager.import_file("a.fit", None, True))["person"], "alex")
        single.close()


class PathSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.m = self.env.manager

    def tearDown(self):
        self.env.close()

    async def key(self, name):
        with self.assertRaises(ArchiveError) as ctx:
            await self.m.import_file(name, "alex", True)
        return ctx.exception.key

    async def test_traversal_and_absolute_paths(self):
        outside = self.env.config_dir / "secret.fit"
        outside.write_bytes(b"x")
        self.assertEqual(await self.key("../../secret.fit"), "file_outside_import_dir")
        self.assertEqual(await self.key(str(outside)), "file_outside_import_dir")
        self.assertEqual(await self.key("/etc/passwd"), "file_outside_import_dir")

    async def test_symlink_out_of_folder(self):
        target = self.env.config_dir / "elsewhere.fit"
        target.write_bytes(b"x")
        os.symlink(target, self.env.import_dir / "link.fit")
        self.assertEqual(await self.key("link.fit"), "file_outside_import_dir")

    async def test_missing_wrong_type_and_directory(self):
        self.assertEqual(await self.key("nope.fit"), "file_not_found")
        (self.env.import_dir / "notes.txt").write_text("hi")
        self.assertEqual(await self.key("notes.txt"), "file_type_not_supported")
        (self.env.import_dir / "folder.fit").mkdir()
        self.assertEqual(await self.key("folder.fit"), "file_not_found")

    async def test_unreadable_file_is_a_clean_error(self):
        (self.env.import_dir / "bad.fit").write_bytes(b"not a fit file at all, just text")
        self.assertEqual(await self.key("bad.fit"), "file_unreadable")

    async def test_import_dir_outside_config_needs_allowlist(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as other:
            self.m.settings = type(self.m.settings)(other, "precise", self.m.settings.people)
            Path(other, "a.fit").write_bytes(b"x")
            self.assertEqual(await self.key("a.fit"), "import_dir_not_allowed")
            self.env.hass.config.allowed.append(str(Path(other).resolve()))
            self.assertEqual(await self.key("a.fit"), "file_unreadable")


class VerifyRollbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.m = self.env.manager

    def tearDown(self):
        self.env.close()

    async def two_runs(self):
        self.env.put_fit("a.fit", seed=3)
        self.env.put_fit("b.fit", seed=5)
        a = await self.m.import_file("a.fit", "alex", False)
        b = await self.m.import_file("b.fit", "sam", False)
        return a, b

    async def test_verify_ok_then_detects_lost_states(self):
        _, b = await self.two_runs()
        ok = await self.m.verify()
        self.assertEqual((ok["checked"], ok["ok"], ok["with_missing_states"]), (2, 2, 0))
        # simulate a purge that removed part of one curve
        rows = self.env.recorder.db["sensor.activity_archive_alex_heart_rate"]
        for ts in sorted(rows)[:10]:
            del rows[ts]
        bad = await self.m.verify()
        self.assertEqual(bad["with_missing_states"], 1)
        self.assertEqual(
            bad["problems"][0]["missing"], {"sensor.activity_archive_alex_heart_rate": 10}
        )
        only_b = await self.m.verify(b["run_id"])
        self.assertEqual((only_b["checked"], only_b["ok"]), (1, 1))
        with self.assertRaises(ArchiveError):
            await self.m.verify("nope")

    async def test_rollback_preview_changes_nothing(self):
        _, b = await self.two_runs()
        before = json.dumps(self.env.hass.storage[LEDGER_STORE_KEY], sort_keys=True)
        preview = await self.m.rollback(b["run_id"], False, True)
        self.assertFalse(preview["confirmed"])
        self.assertFalse(preview["purged"])
        self.assertEqual(
            json.dumps(self.env.hass.storage[LEDGER_STORE_KEY], sort_keys=True), before
        )
        self.assertIn("sensor.activity_archive_sam", preview["purgeable_entities"])

    async def test_rollback_tombstones_and_blocks_reimport_states_stay(self):
        _, b = await self.two_runs()
        self.env.recorder.commit()
        count = sum(len(r) for r in self.env.recorder.db.values())
        result = await self.m.rollback(b["run_id"], True, False)
        self.assertFalse(result["purged"])
        self.assertEqual(sum(len(r) for r in self.env.recorder.db.values()), count)
        again = await self.m.import_file("b.fit", "sam", False)
        self.assertEqual(again["status"], "skipped_deleted")
        verify = await self.m.verify()
        self.assertEqual(verify["checked"], 1, "tombstoned entries are not verified")

    async def test_rollback_purge_touches_only_entities_of_that_run(self):
        _, b = await self.two_runs()
        self.env.recorder.commit()
        alex_before = {e: dict(r) for e, r in self.env.recorder.db.items() if "alex" in e}
        result = await self.m.rollback(b["run_id"], True, True)
        self.assertTrue(result["purged"])
        purged = self.env.hass.services.calls[-1]
        self.assertEqual(purged[:2], ("recorder", "purge_entities"))
        self.assertTrue(all("_sam" in e for e in purged[2]["entity_id"]))
        self.assertEqual(purged[2]["keep_days"], 0)
        self.assertTrue(all("_sam" not in e for e in self.env.recorder.db))
        alex_after = {e: dict(r) for e, r in self.env.recorder.db.items() if "alex" in e}
        self.assertEqual(alex_after, alex_before)

    async def test_rollback_does_not_purge_entities_used_by_other_runs(self):
        self.env.put_fit("a.fit", seed=3)
        self.env.put_fit("c.fit", seed=7)
        await self.m.import_file("a.fit", "alex", False)
        c = await self.m.import_file("c.fit", "alex", False)
        result = await self.m.rollback(c["run_id"], True, True)
        self.assertEqual(result["purgeable_entities"], [])
        self.assertFalse(result["purged"])
        self.assertEqual([c for c in self.env.hass.services.calls if c[1] == "purge_entities"], [])

    async def test_rollback_unknown_run(self):
        with self.assertRaises(ArchiveError) as ctx:
            await self.m.rollback("nope", True, False)
        self.assertEqual(ctx.exception.key, "run_unknown")


class TestStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.m = self.env.manager

    def tearDown(self):
        self.env.close()

    async def test_writes_exactly_one_state_in_2017(self):
        self.env.recorder.keep_days = 10  # the test must work even where retention is short
        result = await self.m.write_test_state()
        self.assertEqual(result["written"], 1)
        self.assertTrue(result["found_in_recorder"])
        self.assertEqual(result["timestamp_utc"], "2017-06-15T12:00:00+00:00")
        self.assertEqual(len(self.env.recorder.events_seen), 1)
        event = self.env.recorder.events_seen[0]
        self.assertEqual(event.data["entity_id"], TEST_ENTITY_ID)
        self.assertEqual(event.data["new_state"].last_updated.timestamp(), TEST_TIMESTAMP)
        reg = self.env.hass.entity_registry.entities[TEST_ENTITY_ID]
        self.assertIn("recorder_longterm", reg.labels)
        self.assertEqual(set(self.env.hass.entity_registry.entities), {TEST_ENTITY_ID})
        # second call writes nothing more
        again = await self.m.write_test_state()
        self.assertEqual((again["written"], again["found_in_recorder"]), (0, True))
        self.assertEqual(len(self.env.recorder.events_seen), 1)

    async def test_not_recorded_blocks(self):
        self.env.recorder.entity_filter = lambda e: False
        from custom_components.activity_archive.core.writer import WriteTargetError

        with self.assertRaises(WriteTargetError):
            await self.m.write_test_state()
        self.assertEqual(self.env.recorder.events_seen, [])

    async def test_remove_touches_only_the_test_entity(self):
        self.env.put_fit()
        await self.m.import_file("a.fit", "alex", False)
        await self.m.write_test_state()
        other_before = {e: dict(r) for e, r in self.env.recorder.db.items() if e != TEST_ENTITY_ID}
        result = await self.m.remove_test_state()
        self.assertEqual((result["remaining_states"], result["unregistered"]), (0, True))
        purge = [c for c in self.env.hass.services.calls if c[1] == "purge_entities"]
        self.assertEqual(
            purge, [("recorder", "purge_entities", {"entity_id": [TEST_ENTITY_ID], "keep_days": 0})]
        )
        self.assertNotIn(TEST_ENTITY_ID, self.env.recorder.db)
        other_after = {e: dict(r) for e, r in self.env.recorder.db.items()}
        self.assertEqual(other_after, other_before)
        self.assertNotIn(TEST_ENTITY_ID, self.env.hass.entity_registry.entities)
        self.assertIn("sensor.activity_archive_alex", self.env.hass.entity_registry.entities)
        # write again works after removal
        self.assertEqual((await self.m.write_test_state())["written"], 1)


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_names_no_paths_no_coordinates(self):
        env = Env()
        try:
            env.put_fit()
            await env.manager.import_file("a.fit", "alex", False)
            data = await env.manager.diagnostics()
            text = json.dumps(data)
            for forbidden in (
                "alex",
                "Alex",
                "sam",
                "Sam",
                "Example",
                str(env.config_dir),
                "a.fit",
                "latitude",
                "lat",
            ):
                self.assertNotIn(forbidden, text, forbidden)
            self.assertEqual(data["settings"]["people"], 2)
            self.assertEqual(data["settings"]["import_dir_name"], "import")
            self.assertEqual(data["ledger"]["entries"], {"complete": 1})
            self.assertEqual(data["entities"]["allowed"], 2 * 9 + 1)
            self.assertEqual(data["recorder"]["backend"], "postgresql")
            from custom_components.activity_archive.diagnostics import (
                async_get_config_entry_diagnostics,
            )

            self.assertEqual(await async_get_config_entry_diagnostics(env.hass, env.entry), data)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
