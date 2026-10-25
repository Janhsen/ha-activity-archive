"""Fake recorder writer for the tests (marker: SYNTHETIC)."""

from __future__ import annotations

from collections.abc import Sequence

from custom_components.activity_archive.core.model import StatePoint
from custom_components.activity_archive.core.writer import (
    StateWriter,
    WriteResult,
    WriteTargetError,
)


class FakeWriter(StateWriter):
    """Stores states in a dict; can fail after N states or refuse entities."""

    def __init__(self) -> None:
        self.store: dict[str, dict[int, StatePoint]] = {}
        self.fail_after: int | None = None  # stop handing over after this many states in total
        self.handed_over = 0
        self.unrecorded: set[str] = set()
        self.backlog_value = 0
        self.drain_per_poll = 0
        self.write_calls: list[tuple[str, int]] = []

    @property
    def backlog(self) -> int:
        return self.backlog_value

    async def sleep(self, _seconds: float) -> None:
        """Replacement for ``asyncio.sleep`` in the throttle: the queue drains while waiting."""
        self.backlog_value = max(0, self.backlog_value - self.drain_per_poll)

    async def check_target(self, entity_id: str) -> None:
        if entity_id in self.unrecorded:
            raise WriteTargetError("not_recorded", entity_id)

    async def existing_timestamps(self, entity_id: str, start_ts: int, end_ts: int) -> set[int]:
        return {ts for ts in self.store.get(entity_id, {}) if start_ts <= ts <= end_ts}

    async def write(self, entity_id: str, states: Sequence[StatePoint]) -> WriteResult:
        stamps = [s.ts for s in states]
        assert stamps == sorted(stamps), "states must be handed over in ascending order"
        self.write_calls.append((entity_id, len(states)))
        done = 0
        for state in states:
            if self.fail_after is not None and self.handed_over >= self.fail_after:
                return WriteResult(done, "simulated failure")
            assert state.ts not in self.store.get(entity_id, {}), "a state was written twice"
            self.store.setdefault(entity_id, {})[state.ts] = state
            self.handed_over += 1
            done += 1
        if self.backlog_value or self.drain_per_poll:
            self.backlog_value += done
        return WriteResult(done)

    def snapshot(self) -> dict[str, dict[int, tuple[str, tuple]]]:
        return {
            e: {ts: (s.state, s.attributes) for ts, s in sorted(d.items())}
            for e, d in sorted(self.store.items())
        }

    def total(self) -> int:
        return sum(len(d) for d in self.store.values())
