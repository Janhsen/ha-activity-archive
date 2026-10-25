"""Runtime object of the config entry: ledger, writer, lock and the actions behind the services."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.components import recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import issues
from .adapters.base import SourceActivity
from .const import (
    DOMAIN,
    IMPORT_SUFFIXES,
    SOURCE_FILE,
    TEST_ENTITY_ID,
    TEST_STATE,
    TEST_TIMESTAMP,
    VERIFY_PAD_S,
)
from .core import importer, loader
from .core.ledger import (
    LEDGER_VERSION,
    STATUS_COMPLETE,
    STATUS_WRITING,
    Ledger,
    RunInProgressError,
)
from .core.model import ReaderError, StatePoint
from .core.plan import SkippedError
from .core.writer import StateWriter
from .entities import all_entity_ids, build_catalog
from .ledger_store import HaLedgerStore
from .recorder_writer import RecorderStateWriter
from .settings import Settings, parse_settings

_LOGGER = logging.getLogger(__name__)

MAX_PROBLEMS = 50


class ArchiveError(Exception):
    """A request cannot be carried out; ``key`` is the translation key of the message."""

    def __init__(self, key: str, placeholders: dict[str, str] | None = None) -> None:
        """Remember the translation key and its placeholders."""
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders or {}


def _resolve_import_file(
    import_dir: str, config_dir: str, is_allowed: Callable[[str], bool], name: str
) -> Path:
    """Blocking: return the file inside the import folder or raise :class:`ArchiveError`.

    The folder itself has to be below the configuration directory or allowed through
    ``allowlist_external_dirs``; the file has to stay inside the folder after symbolic links
    are resolved.
    """
    base = Path(import_dir).resolve()
    inside_config = base.is_relative_to(Path(config_dir).resolve())
    if not (inside_config or is_allowed(str(base))):
        raise ArchiveError("import_dir_not_allowed")
    if not base.is_dir():
        raise ArchiveError("import_dir_missing")
    candidate = (base / name).resolve()
    if not candidate.is_relative_to(base):
        raise ArchiveError("file_outside_import_dir")
    if not candidate.is_file():
        raise ArchiveError("file_not_found")
    if candidate.suffix.lower() not in IMPORT_SUFFIXES:
        raise ArchiveError("file_type_not_supported")
    return candidate


def _read_file(path: Path) -> Any:
    return loader.load_path(path)


class ArchiveManager:
    """Everything one config entry needs at run time."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        """Read the settings and prepare writers; nothing touches the recorder yet."""
        self.hass = hass
        self.entry = entry
        self.settings: Settings = parse_settings(entry.data, entry.options)
        self.catalog = build_catalog(self.settings.people)
        self.store = HaLedgerStore(hass)
        self.lock = asyncio.Lock()
        self._ledger: Ledger | None = None
        self._ledger_lock = asyncio.Lock()
        self.writer: StateWriter = RecorderStateWriter(hass, entry, self.catalog)
        self._test_writer: StateWriter = RecorderStateWriter(
            hass, entry, self.catalog, enforce_retention=False
        )

    # ------------------------------------------------------------------ set-up
    async def async_setup(self) -> None:
        """Create the import folder if it is missing (best effort)."""
        try:
            await self.hass.async_add_executor_job(
                lambda: Path(self.settings.import_dir).mkdir(parents=True, exist_ok=True)
            )
        except OSError as err:
            _LOGGER.warning("Import folder could not be created: %s", type(err).__name__)

    async def async_run_checks(self) -> list[str]:
        """Run the start checks; returns the ids of the issues found."""
        findings = await issues.async_run_checks(self.hass, all_entity_ids(self.settings.people))
        return [f.key for f in findings]

    async def async_ledger(self) -> Ledger:
        """Return the ledger (loaded once). An unknown file version stops everything.

        Replacing an unreadable ledger by an empty one would import duplicates, so the file is
        left alone and a repair issue explains it.
        """
        async with self._ledger_lock:
            if self._ledger is None:
                data = await self.store.async_load()
                if data is not None and (
                    data.get("v") != LEDGER_VERSION or not isinstance(data.get("entries"), dict)
                ):
                    issues.set_ledger_issue(self.hass, data.get("v"))
                    raise ArchiveError("ledger_unreadable", {"version": str(data.get("v"))})
                issues.clear_ledger_issue(self.hass)
                ledger = Ledger(self.store, data)
                # No run can be alive in a process that has just started: resume instead of waiting.
                if ledger.abort_stale_run(time.time()):
                    await ledger.save()
                self._ledger = ledger
            return self._ledger

    # ------------------------------------------------------------------ helpers
    def _person(self, slug: str | None) -> Any:
        if not self.settings.people:
            raise ArchiveError("no_person")
        person = self.settings.person(slug)
        if person is None:
            raise ArchiveError("person_unknown" if slug else "person_required")
        return person

    def _busy(self) -> None:
        if self.lock.locked():
            raise ArchiveError("run_in_progress")

    # ------------------------------------------------------------------ import_file
    async def import_file(self, file: str, person: str | None, dry_run: bool) -> dict[str, Any]:
        """Import one activity file from the import folder (or only read and count it)."""
        who = self._person(person)
        path = await self.hass.async_add_executor_job(
            _resolve_import_file,
            self.settings.import_dir,
            self.hass.config.config_dir,
            self.hass.config.is_allowed_path,
            file,
        )
        try:
            raw = await self.hass.async_add_executor_job(_read_file, path)
        except ReaderError as err:
            raise ArchiveError("file_unreadable", {"error": str(err)}) from err
        except OSError as err:
            raise ArchiveError("file_unreadable", {"error": type(err).__name__}) from err
        activity = SourceActivity(SOURCE_FILE, None, raw, origin=path.name)
        try:
            item = activity.build_item(
                person=who.slug,
                profile=self.settings.profile,
                assumed_tz=dt_util.get_default_time_zone(),
            )
        except SkippedError as err:
            return {"dry_run": dry_run, "status": "skipped", "reason": str(err)}

        ledger = await self.async_ledger()
        if dry_run:
            outcome = await importer.import_item(
                item, ledger=ledger, writer=self.writer, run_id="dry-run", dry_run=True
            )
            run_id = None
        else:
            self._busy()
            async with self.lock:
                try:
                    report = await importer.import_batch(
                        [item], ledger=ledger, writer=self.writer, dry_run=False
                    )
                except RunInProgressError as err:
                    raise ArchiveError("run_in_progress") from err
            outcome, run_id = report.outcomes[0], report.run_id
        result = self._outcome_dict(item, outcome, dry_run, run_id)
        _LOGGER.info(
            "import_file %s: status=%s states=%s new=%s",
            "dry run" if dry_run else "run",
            outcome.status,
            result["total_states"],
            outcome.new_states,
        )
        return result

    def _outcome_dict(
        self,
        item: importer.ImportItem,
        outcome: importer.ImportOutcome,
        dry_run: bool,
        run_id: str | None,
    ) -> dict[str, Any]:
        plan = item.plan
        return {
            "dry_run": dry_run,
            "status": outcome.status,
            "reason": outcome.reason,
            "run_id": run_id,
            "key": outcome.key,
            "person": plan.person,
            "sport": plan.sport.sport,
            "start_utc": datetime.fromtimestamp(plan.start, UTC).isoformat(),
            "duration_s": plan.end - plan.start,
            "profile": self.settings.profile,
            "planned": dict(outcome.planned),
            "total_states": sum(outcome.planned.values()),
            "new_states": outcome.new_states,
            "warnings": list(plan.warnings),
        }

    # ------------------------------------------------------------------ verify
    async def verify(self, run_id: str | None = None) -> dict[str, Any]:
        """Compare the ledger with the recorder (are all states of complete entries there?)."""
        ledger = await self.async_ledger()
        if run_id is not None and run_id not in ledger.data["runs"]:
            raise ArchiveError("run_unknown", {"run_id": run_id})
        if run_id is None:
            entries = [{"key": k, **e} for k, e in ledger.data["entries"].items()]
        else:
            entries = ledger.run_manifest(run_id)
        checked = ok = 0
        problems: list[dict[str, Any]] = []
        interrupted = [e["key"] for e in entries if e["status"] == STATUS_WRITING]
        for entry in entries:
            if entry["status"] != STATUS_COMPLETE:
                continue
            checked += 1
            missing: dict[str, int] = {}
            for entity, expected in entry.get("written", {}).items():
                found = await self.writer.existing_timestamps(
                    entity, entry["start"] - 1, entry["end"] + VERIFY_PAD_S
                )
                if len(found) < expected:
                    missing[entity] = expected - len(found)
            if missing:
                if len(problems) < MAX_PROBLEMS:
                    problems.append({"key": entry["key"], "missing": missing})
            else:
                ok += 1
            await asyncio.sleep(0)
        return {
            "checked": checked,
            "ok": ok,
            "with_missing_states": checked - ok,
            "interrupted": len(interrupted),
            "problems": problems,
            "runs": {
                rid: {"status": run["status"], "entries": len(run["entries"])}
                for rid, run in list(ledger.data["runs"].items())[-10:]
            },
        }

    # ------------------------------------------------------------------ rollback
    async def rollback(self, run_id: str, confirm: bool, purge_entities: bool) -> dict[str, Any]:
        """Take a run back: tombstone its entries; optionally purge entities only it used."""
        self._busy()
        async with self.lock:
            ledger = await self.async_ledger()
            if run_id not in ledger.data["runs"]:
                raise ArchiveError("run_unknown", {"run_id": run_id})
            manifest = ledger.run_manifest(run_id)
            used_elsewhere = ledger.entities_in_use_elsewhere(run_id)
            entities = sorted({e for entry in manifest for e in entry.get("entities", ())})
            purgeable = [e for e in entities if e not in used_elsewhere]
            result: dict[str, Any] = {
                "confirmed": confirm,
                "run_id": run_id,
                "entries": len(manifest),
                "entities": entities,
                "purgeable_entities": purgeable,
                "kept_because_used_by_other_runs": sorted(set(entities) - set(purgeable)),
                "purged": False,
                "note": (
                    "The states stay in the recorder unless purge_entities is set; "
                    "Home Assistant cannot delete a time window."
                ),
            }
            if not confirm:
                return result
            try:
                ledger.rollback_run(run_id, time.time())
            except RunInProgressError as err:
                raise ArchiveError("run_in_progress") from err
            await ledger.save()
            if purge_entities and purgeable:
                await self._purge(purgeable)
                result["purged"] = True
            return result

    async def _purge(self, entity_ids: list[str]) -> None:
        """Delete all states of the given (own) entities with the recorder's own service."""
        await self.hass.services.async_call(
            recorder.DOMAIN,
            "purge_entities",
            {"entity_id": entity_ids, "keep_days": 0},
            blocking=True,
        )
        await recorder.get_instance(self.hass).async_block_till_done()

    # ------------------------------------------------------------------ E0 test
    async def write_test_state(self) -> dict[str, Any]:
        """Write exactly one state (2017-06-15 12:00 UTC) to ``sensor.activity_archive_test``."""
        self._busy()
        async with self.lock:
            await self._test_writer.check_target(TEST_ENTITY_ID)
            before = await self._test_writer.existing_timestamps(
                TEST_ENTITY_ID, TEST_TIMESTAMP, TEST_TIMESTAMP
            )
            written = 0
            if TEST_TIMESTAMP not in before:
                point = StatePoint(TEST_TIMESTAMP, TEST_STATE, (("test", True),))
                outcome = await self._test_writer.write(TEST_ENTITY_ID, [point])
                if outcome.error:
                    raise ArchiveError("test_write_failed", {"error": outcome.error})
                written = outcome.done
            after = await self._test_writer.existing_timestamps(
                TEST_ENTITY_ID, TEST_TIMESTAMP, TEST_TIMESTAMP
            )
            instance = recorder.get_instance(self.hass)
            return {
                "entity_id": TEST_ENTITY_ID,
                "timestamp_utc": datetime.fromtimestamp(TEST_TIMESTAMP, UTC).isoformat(),
                "written": written,
                "found_in_recorder": TEST_TIMESTAMP in after,
                "recorder_keep_days": getattr(instance, "keep_days", None),
                "recorder_auto_purge": getattr(instance, "auto_purge", None),
                "hint": (
                    "Check the history of the entity after the next night purge; "
                    "the state survives only if retention and the label protect it."
                ),
            }

    async def remove_test_state(self) -> dict[str, Any]:
        """Remove the test state again: purge the test entity only, then unregister it."""
        self._busy()
        async with self.lock:
            await self._purge([TEST_ENTITY_ID])
            left = await self._test_writer.existing_timestamps(
                TEST_ENTITY_ID, TEST_TIMESTAMP - 1, TEST_TIMESTAMP + 1
            )
            registry = er.async_get(self.hass)
            unregistered = False
            if not left and registry.async_get(TEST_ENTITY_ID) is not None:
                registry.async_remove(TEST_ENTITY_ID)
                unregistered = True
            return {
                "entity_id": TEST_ENTITY_ID,
                "remaining_states": len(left),
                "unregistered": unregistered,
            }

    # ------------------------------------------------------------------ diagnostics
    async def diagnostics(self) -> dict[str, Any]:
        """Return counts and settings, without names, paths, coordinates or activity data."""
        try:
            ledger_info: Any = (await self.async_ledger()).summary()
        except ArchiveError as err:
            ledger_info = {"error": err.key}
        facts = await issues.async_collect_facts(self.hass, all_entity_ids(self.settings.people))
        registry = er.async_get(self.hass)
        registered = [e for e in self.catalog if registry.async_get(e) is not None]
        return {
            "settings": {
                "profile": self.settings.profile,
                "people": len(self.settings.people),
                "import_dir_name": Path(self.settings.import_dir).name,
            },
            "ledger": ledger_info,
            "busy": self.lock.locked(),
            "recorder": {
                "keep_days": facts.keep_days,
                "auto_purge": facts.auto_purge,
                "backend": facts.dialect,
                "recording": facts.recording,
                "entities_not_recorded": len(facts.unrecorded),
                "queue_backlog": self.writer.backlog,
            },
            "disk_free_gb": None
            if facts.free_bytes is None
            else round(facts.free_bytes / 2**30, 1),
            "entities": {"allowed": len(self.catalog), "registered": len(registered)},
            "domain": DOMAIN,
        }
