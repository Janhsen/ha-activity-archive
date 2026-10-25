"""People slugs, start-check evaluation and ledger rollback (marker: SYNTHETIC)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from custom_components.activity_archive.const import MIN_FREE_BYTES, MIN_PURGE_KEEP_DAYS
from custom_components.activity_archive.core import health
from custom_components.activity_archive.core.ledger import (
    Ledger,
    MemoryLedgerStore,
    RunInProgressError,
)
from custom_components.activity_archive.core.people import slugify, validate_slug
from custom_components.activity_archive.core.plan import ENTITY_SUFFIX, entity_for, position_entity

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "activity_archive"


class SlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Alex Example"), "alex_example")
        self.assertEqual(slugify("Jörg Müller"), "joerg_mueller")
        self.assertEqual(len(slugify("Abcdefghij Klmnopqrst Uvwxyz")), 24)
        self.assertEqual(slugify("  !!  "), "")
        self.assertEqual(slugify("Åsa"), "asa")

    def test_valid_and_invalid(self):
        self.assertIsNone(validate_slug("alex"))
        self.assertIsNone(validate_slug("alex_2"))
        for bad in ("", "Alex", "a b", "_a", "a__b", "a" * 25, "ä"):
            self.assertEqual(validate_slug(bad), "invalid_slug", bad)

    def test_existing(self):
        self.assertEqual(validate_slug("alex", ["alex"]), "slug_exists")

    def test_reserved_cannot_collide_with_a_curve_entity(self):
        for word in (*ENTITY_SUFFIX.values(), "position", "test"):
            self.assertEqual(validate_slug(word), "slug_reserved", word)
            self.assertEqual(validate_slug(f"alex_{word}"), "slug_reserved", word)

    def test_no_two_valid_people_share_an_entity_id(self):
        slugs = ["alex", "alex_heart", "alex_h", "sam", "heart", "rate"]
        slugs = [s for s in slugs if validate_slug(s) is None]
        ids = []
        for slug in slugs:
            ids.append(f"sensor.activity_archive_{slug}")
            ids += [entity_for(slug, k) for k in ENTITY_SUFFIX]
            ids.append(position_entity(slug))
        self.assertEqual(len(ids), len(set(ids)))


class HealthTests(unittest.TestCase):
    def keys(self, **kw):
        return [f.key for f in health.evaluate(health.HealthFacts(**kw))]

    def test_clean_install(self):
        self.assertEqual(
            self.keys(
                keep_days=MIN_PURGE_KEEP_DAYS, dialect="postgresql", free_bytes=MIN_FREE_BYTES
            ),
            [],
        )

    def test_retention(self):
        found = health.evaluate(health.HealthFacts(keep_days=10))
        self.assertEqual(found[0].key, health.ISSUE_RETENTION)
        self.assertEqual(found[0].placeholders["keep_days"], "10")
        self.assertEqual(found[0].severity, "error")

    def test_retention_ignored_without_auto_purge_or_unknown(self):
        self.assertEqual(self.keys(keep_days=10, auto_purge=False), [])
        self.assertEqual(self.keys(keep_days=None), [])

    def test_filter(self):
        found = health.evaluate(
            health.HealthFacts(unrecorded=tuple(f"sensor.x{i}" for i in range(6)))
        )
        self.assertEqual(found[0].key, health.ISSUE_FILTER)
        self.assertEqual(found[0].placeholders["count"], "6")
        self.assertIn("(+2)", found[0].placeholders["entities"])

    def test_backend_and_disk_are_warnings(self):
        found = health.evaluate(health.HealthFacts(dialect="sqlite", free_bytes=1 * 2**30))
        self.assertEqual(
            {f.key: f.severity for f in found},
            {"database_backend": "warning", "low_disk_space": "warning"},
        )

    def test_recorder_off_first(self):
        found = health.evaluate(health.HealthFacts(recording=False, keep_days=1))
        self.assertEqual(found[0].key, health.ISSUE_RECORDER_OFF)


class RollbackTests(unittest.IsolatedAsyncioTestCase):
    def add(self, ledger, key, run, entities, status="complete"):
        ledger.start_entry(
            key, run_id=run, source="file", source_id=None, content_hash=key, activity_id=key,
            start=100, end=200, entities=entities,
        )  # fmt: skip
        if status == "complete":
            ledger.complete_entry(key, {e: 3 for e in entities})

    async def test_rollback_tombstones_only_that_run(self):
        ledger = Ledger(MemoryLedgerStore())
        r1 = ledger.begin_run(1000.0)
        self.add(ledger, "file:a", r1, ["sensor.e1", "sensor.e2"])
        ledger.end_run(r1, 1001.0)
        r2 = ledger.begin_run(2000.0)
        self.add(ledger, "file:b", r2, ["sensor.e2", "sensor.e3"])
        self.add(ledger, "file:c", r2, ["sensor.e3"], status="writing")
        ledger.end_run(r2, 2001.0, "stopped")
        self.assertEqual(ledger.entities_in_use_elsewhere(r2), {"sensor.e1", "sensor.e2"})
        changed = ledger.rollback_run(r2, 3000.0)
        self.assertEqual(sorted(changed), ["file:b", "file:c"])
        self.assertEqual(ledger.status("file:a"), "complete")
        self.assertEqual(ledger.status("file:b"), "tombstone")
        self.assertEqual(ledger.data["runs"][r2]["status"], "rolled_back")
        self.assertEqual(ledger.rollback_run(r2, 3001.0), [], "second rollback changes nothing")
        # the tombstoned activity is not imported again
        self.assertEqual(ledger.find("file", None, "file:b"), "file:b")

    async def test_active_run_cannot_be_rolled_back(self):
        ledger = Ledger(MemoryLedgerStore())
        run = ledger.begin_run(1000.0)
        with self.assertRaises(RunInProgressError):
            ledger.rollback_run(run, 1001.0)
        with self.assertRaises(KeyError):
            ledger.rollback_run("nope", 1.0)

    async def test_abort_stale_run_lets_the_next_run_start(self):
        ledger = Ledger(MemoryLedgerStore())
        run = ledger.begin_run(1000.0)
        with self.assertRaises(RunInProgressError):
            ledger.begin_run(1001.0)
        self.assertEqual(ledger.abort_stale_run(1002.0), run)
        self.assertEqual(ledger.data["runs"][run]["status"], "aborted")
        ledger.begin_run(1003.0)
        self.assertIsNone(Ledger(MemoryLedgerStore()).abort_stale_run(5.0))

    async def test_summary_has_counts_only(self):
        ledger = Ledger(MemoryLedgerStore())
        run = ledger.begin_run(1000.0)
        self.add(ledger, "file:a", run, ["sensor.e1"])
        text = repr(ledger.summary())
        self.assertNotIn("file:a", text)
        self.assertEqual(ledger.summary()["entries"], {"complete": 1})


class IsolationTests(unittest.TestCase):
    def test_core_and_adapters_do_not_import_home_assistant(self):
        for folder in ("core", "adapters"):
            for path in (PKG / folder).glob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    for name in names:
                        self.assertFalse(
                            name.split(".")[0] in {"homeassistant", "voluptuous"},
                            f"{path.name} imports {name}",
                        )


if __name__ == "__main__":
    unittest.main()
