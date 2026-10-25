"""Import one planned activity into the recorder, resumable and idempotent.

The order is deliberate: curves first, the activity sensor last, so that an activity in the list
always has its curves. Before every entity the existing time stamps are read from the database
and only the missing states are written; the ledger status ``complete`` is set only after the
last state was handed over. A run interrupted at any point therefore ends in the same state
after the next run, and a second run writes nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .ledger import STATUS_COMPLETE, STATUS_TOMBSTONE, Ledger
from .plan import ActivityPlan
from .writer import Sleep, StateWriter, WriteTargetError, write_throttled

WRITTEN = "written"
SKIPPED_KNOWN = "skipped_known"
SKIPPED_DELETED = "skipped_deleted"
BLOCKED = "blocked"
FAILED = "failed"
DRY_RUN = "dry_run"


def entry_key(source: str, source_id: str | None, content_hash: str | None) -> str:
    """Return the ledger key of a source activity: source plus its id, else its file hash."""
    ident = source_id or content_hash
    if not ident:
        raise ValueError("an activity needs a source id or a content hash")
    return f"{source}:{ident}"


@dataclass(frozen=True, slots=True)
class ImportItem:
    """A plan together with its identity in the ledger."""

    plan: ActivityPlan
    source: str
    source_id: str | None = None
    content_hash: str | None = None

    @property
    def key(self) -> str:
        """Ledger key."""
        return entry_key(self.source, self.source_id, self.content_hash)


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What happened to one activity."""

    key: str
    status: str
    planned: dict[str, int] = field(default_factory=dict)
    new_states: int = 0
    reason: str | None = None


async def import_item(
    item: ImportItem,
    *,
    ledger: Ledger,
    writer: StateWriter,
    run_id: str,
    dry_run: bool = False,
    sleep: Sleep = asyncio.sleep,
) -> ImportOutcome:
    """Write the missing states of one activity and update the ledger."""
    key, plan = item.key, item.plan
    status = ledger.status(key)
    if status == STATUS_COMPLETE:
        return ImportOutcome(key, SKIPPED_KNOWN, reason="already imported")
    if status == STATUS_TOMBSTONE:
        return ImportOutcome(key, SKIPPED_DELETED, reason="deleted on purpose")
    known = ledger.find(item.source, item.source_id, item.content_hash)
    if known is not None and known != key and ledger.status(known) == STATUS_COMPLETE:
        return ImportOutcome(key, SKIPPED_KNOWN, reason=f"same activity as {known}")

    states = plan.entity_states()
    planned = {entity: len(points) for entity, points in states.items()}
    if dry_run:
        return ImportOutcome(key, DRY_RUN, planned)
    try:
        for entity in states:
            await writer.check_target(entity)
    except WriteTargetError as err:
        return ImportOutcome(key, BLOCKED, planned, reason=f"{err.key}: {err.entity_id}")

    ledger.start_entry(
        key,
        run_id=run_id,
        source=item.source,
        source_id=item.source_id,
        content_hash=item.content_hash,
        activity_id=plan.activity_id,
        start=plan.start,
        end=plan.end,
        entities=list(states),
    )
    await ledger.save()
    stored: dict[str, int] = {}
    new_states = 0
    for entity, points in states.items():
        existing = await writer.existing_timestamps(entity, points[0].ts, points[-1].ts)
        missing = [p for p in points if p.ts not in existing]
        result = await write_throttled(writer, entity, missing, sleep=sleep)
        new_states += result.done
        if result.error:
            return ImportOutcome(key, FAILED, planned, new_states, result.error)
        stored[entity] = len(points)
    ledger.complete_entry(key, stored)
    await ledger.save()
    return ImportOutcome(key, WRITTEN, planned, new_states)


@dataclass(frozen=True, slots=True)
class BatchReport:
    """Result of an import run."""

    run_id: str
    outcomes: tuple[ImportOutcome, ...]

    def count(self, status: str) -> int:
        """Return how many activities ended with ``status``."""
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def new_states(self) -> int:
        """Total number of states written in this run."""
        return sum(o.new_states for o in self.outcomes)


async def import_batch(
    items: Iterable[ImportItem],
    *,
    ledger: Ledger,
    writer: StateWriter,
    clock: Callable[[], float] = time.time,
    run_id: str | None = None,
    dry_run: bool = False,
    sleep: Sleep = asyncio.sleep,
) -> BatchReport:
    """Import activities one after the other inside one run.

    Raises :class:`~.ledger.RunInProgressError` if another run is active. The run stops at the
    first blocked or failed activity, because the cause (retention, queue) affects all of them.
    """
    run = ledger.begin_run(clock(), run_id)
    outcomes: list[ImportOutcome] = []
    status = "done"
    try:
        for item in items:
            outcome = await import_item(
                item, ledger=ledger, writer=writer, run_id=run, dry_run=dry_run, sleep=sleep
            )
            outcomes.append(outcome)
            ledger.heartbeat(run, clock())
            if outcome.status in (BLOCKED, FAILED):
                status = "stopped"
                break
    except BaseException:
        status = "aborted"
        raise
    finally:
        ledger.end_run(run, clock(), status)
        await ledger.save()
    return BatchReport(run, tuple(outcomes))
