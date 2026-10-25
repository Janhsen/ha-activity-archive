"""Ledger and run manifest: what was imported, by which run, and what was deleted on purpose.

The recorder database is the source of truth for *values* (the importer reads the existing time
stamps before it writes), the ledger only remembers *decisions*: which source activity became
which archive activity, which run wrote it, and which activities are tombstoned so that a
deleted activity does not return on the next import.

An entry has three states: ``writing`` (started, possibly interrupted), ``complete`` (all
states handed to the recorder) and ``tombstone``. Only ``complete`` and ``tombstone`` stop a
re-import; ``writing`` is resumed and the database tells what is still missing.

Persistence goes through :class:`LedgerStore`. Home Assistant's ``.storage`` implementation
is ``ledger_store.HaLedgerStore``; tests use :class:`MemoryLedgerStore`.
"""

from __future__ import annotations

import copy
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Final

LEDGER_VERSION: Final = 1
STALE_RUN_SECONDS: Final = 6 * 3600  # a run without progress for this long may be replaced

STATUS_WRITING: Final = "writing"
STATUS_COMPLETE: Final = "complete"
STATUS_TOMBSTONE: Final = "tombstone"


class RunInProgressError(Exception):
    """Another import run is active; a second one is refused."""


class LedgerStore(ABC):
    """Persistence behind the ledger (Home Assistant ``Store`` in production)."""

    @abstractmethod
    async def async_load(self) -> dict[str, Any] | None:
        """Return the saved data or ``None`` if nothing was saved yet."""

    @abstractmethod
    async def async_save(self, data: dict[str, Any]) -> None:
        """Save a complete snapshot."""


class MemoryLedgerStore(LedgerStore):
    """Keeps the snapshot in memory; saves are deep copies, loads return copies."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        """Start with optional existing data."""
        self.data = copy.deepcopy(data)
        self.saves = 0

    async def async_load(self) -> dict[str, Any] | None:
        """Return a copy of the saved snapshot."""
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, Any]) -> None:
        """Remember a copy of ``data``."""
        self.data = copy.deepcopy(data)
        self.saves += 1


def _empty() -> dict[str, Any]:
    return {"v": LEDGER_VERSION, "entries": {}, "runs": {}, "active_run": None}


class Ledger:
    """Entries per source activity and a manifest per run."""

    def __init__(self, store: LedgerStore, data: dict[str, Any] | None = None) -> None:
        """Wrap ``data`` (a loaded snapshot); wrong version or shape gives an empty ledger."""
        ok = isinstance(data, dict) and data.get("v") == LEDGER_VERSION and "entries" in data
        self._store = store
        self.data: dict[str, Any] = data if ok and data is not None else _empty()
        self._by_alias: dict[tuple[str, str], str] = {}
        self._by_hash: dict[str, str] = {}
        self._reindex()

    @classmethod
    async def load(cls, store: LedgerStore) -> Ledger:
        """Load the ledger from ``store``."""
        return cls(store, await store.async_load())

    async def save(self) -> None:
        """Write the snapshot to the store."""
        await self._store.async_save(self.data)

    # ------------------------------------------------------------------ entries
    def _reindex(self) -> None:
        self._by_alias.clear()
        self._by_hash.clear()
        for key, entry in self.data["entries"].items():
            if entry.get("source_id"):
                self._by_alias[(entry["source"], entry["source_id"])] = key
            if entry.get("content_hash"):
                self._by_hash[entry["content_hash"]] = key

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the entry for ``key``."""
        return self.data["entries"].get(key)

    def find(self, source: str, source_id: str | None, content_hash: str | None) -> str | None:
        """Return the key of a known entry for the same source id or the same file content."""
        if source_id and (source, source_id) in self._by_alias:
            return self._by_alias[(source, source_id)]
        if content_hash and content_hash in self._by_hash:
            return self._by_hash[content_hash]
        return None

    def status(self, key: str) -> str | None:
        """Return ``writing``, ``complete``, ``tombstone`` or ``None``."""
        entry = self.get(key)
        return entry["status"] if entry else None

    def start_entry(
        self,
        key: str,
        *,
        run_id: str,
        source: str,
        source_id: str | None,
        content_hash: str | None,
        activity_id: str,
        start: int,
        end: int,
        entities: Sequence[str],
    ) -> None:
        """Record that writing of ``key`` begins; an interrupted entry is registered anew.

        A complete or tombstoned entry is left untouched.
        """
        entry = self.data["entries"].get(key)
        if entry is None or entry["status"] == STATUS_WRITING:
            self.data["entries"][key] = {
                "status": STATUS_WRITING,
                "run": run_id,
                "source": source,
                "source_id": source_id,
                "content_hash": content_hash,
                "activity_id": activity_id,
                "start": start,
                "end": end,
                "entities": list(entities),
                "written": {},
            }
            run = self.data["runs"].get(run_id)
            if run is not None and key not in run["entries"]:
                run["entries"].append(key)
            self._reindex()

    def complete_entry(self, key: str, written: dict[str, int]) -> None:
        """Mark all states of ``key`` as handed to the recorder (``written``: states per entity)."""
        entry = self.data["entries"][key]
        entry["status"] = STATUS_COMPLETE
        entry["written"] = dict(written)

    def tombstone(self, key: str) -> None:
        """Remember that the activity was deleted on purpose: it is not imported again."""
        entry = self.data["entries"].setdefault(
            key, {"source": "", "source_id": None, "content_hash": None, "entities": []}
        )
        entry["status"] = STATUS_TOMBSTONE
        self._reindex()

    # ------------------------------------------------------------------ runs
    def active_run(self, now: float) -> str | None:
        """Return the id of the active run, ignoring a stale one."""
        run_id = self.data.get("active_run")
        run = self.data["runs"].get(run_id) if run_id else None
        if run and now - run["heartbeat"] <= STALE_RUN_SECONDS:
            return run_id
        return None

    def begin_run(self, now: float, run_id: str | None = None) -> str:
        """Start a run; raises :class:`RunInProgressError` while another one is active."""
        if self.active_run(now):
            raise RunInProgressError("another import run is active")
        run_id = run_id or f"{int(now)}-{uuid.uuid4().hex[:8]}"
        self.data["runs"][run_id] = {
            "started": now,
            "heartbeat": now,
            "ended": None,
            "status": "running",
            "entries": [],
        }
        self.data["active_run"] = run_id
        return run_id

    def heartbeat(self, run_id: str, now: float) -> None:
        """Tell that the run is alive (call between activities)."""
        self.data["runs"][run_id]["heartbeat"] = now

    def end_run(self, run_id: str, now: float, status: str = "done") -> None:
        """Finish a run."""
        run = self.data["runs"][run_id]
        run["ended"], run["status"] = now, status
        if self.data.get("active_run") == run_id:
            self.data["active_run"] = None

    def rollback_run(self, run_id: str, now: float) -> list[str]:
        """Tombstone every entry of a finished run so that none of it is imported again.

        Returns the keys that were changed. The states already in the recorder stay: Home
        Assistant cannot delete a time window. Raises ``KeyError`` for an unknown run and
        :class:`RunInProgressError` for the active one.
        """
        run = self.data["runs"][run_id]
        if self.active_run(now) == run_id:
            raise RunInProgressError("the run is still active")
        changed = [
            k for k in run["entries"] if self.data["entries"][k]["status"] != STATUS_TOMBSTONE
        ]
        for key in changed:
            self.tombstone(key)
        run["status"] = "rolled_back"
        return changed

    def entities_in_use_elsewhere(self, run_id: str) -> set[str]:
        """Entities that entries outside ``run_id`` (not tombstoned) still count on."""
        inside = set(self.data["runs"][run_id]["entries"])
        return {
            entity
            for key, entry in self.data["entries"].items()
            if key not in inside and entry["status"] != STATUS_TOMBSTONE
            for entity in entry.get("entities", ())
        }

    def abort_stale_run(self, now: float) -> str | None:
        """End a run left over from a stopped process; returns its id.

        Call at start-up: no run can be alive in a process that was just started, so the lock
        must not keep the next run out for hours. The interrupted entry is resumed by the next
        import (the recorder tells what is missing).
        """
        run_id = self.data.get("active_run")
        if run_id and run_id in self.data["runs"]:
            self.end_run(run_id, now, "aborted")
            return run_id
        self.data["active_run"] = None
        return None

    def summary(self) -> dict[str, Any]:
        """Return counts only (no keys, no activity times): safe for diagnostics."""
        statuses: dict[str, int] = {}
        for entry in self.data["entries"].values():
            statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
        run_status: dict[str, int] = {}
        for run in self.data["runs"].values():
            run_status[run["status"]] = run_status.get(run["status"], 0) + 1
        return {"version": self.data["v"], "entries": statuses, "runs": run_status}

    def run_manifest(self, run_id: str) -> list[dict[str, Any]]:
        """Return what a run wrote: entries with entities and time window (for rollback lists)."""
        keys = self.data["runs"][run_id]["entries"]
        return [{"key": key, **self.data["entries"][key]} for key in keys]
