"""Interface to the recorder writer, plus the backlog brake that works with any implementation.

The real writer (``recorder_writer.RecorderStateWriter``) puts hand-built ``state_changed`` events
on the recorder queue the way ``garmin_personal`` does. This module only defines what the
importer needs from it so that the core stays free of Home Assistant and testable with a fake.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..const import (
    BACKLOG_HARD_LIMIT,
    BACKLOG_PAUSE_ABOVE,
    BACKLOG_RESUME_BELOW,
    WRITE_BLOCK_SIZE,
)
from .model import StatePoint


class WriteTargetError(Exception):
    """An entity cannot take states (missing, disabled, not recorded, retention too short)."""

    def __init__(self, key: str, entity_id: str = "") -> None:
        """Remember the translation key and the entity for the message."""
        super().__init__(key)
        self.key = key
        self.entity_id = entity_id


@dataclass(frozen=True, slots=True)
class WriteResult:
    """``done`` states were handed over; ``error`` tells why it stopped early."""

    done: int
    error: str | None = None


class StateWriter(ABC):
    """What the importer needs from a recorder writer."""

    @property
    @abstractmethod
    def backlog(self) -> int:
        """Number of entries waiting in the recorder queue."""

    @abstractmethod
    async def check_target(self, entity_id: str) -> None:
        """Raise :class:`WriteTargetError` unless ``entity_id`` can take and keep states."""

    @abstractmethod
    async def existing_timestamps(self, entity_id: str, start_ts: int, end_ts: int) -> set[int]:
        """Return the time stamps already stored for the entity in ``start_ts..end_ts``.

        Implementations must first let the recorder commit what was queued before, otherwise a
        resumed import would not see its own earlier writes.
        """

    @abstractmethod
    async def write(self, entity_id: str, states: Sequence[StatePoint]) -> WriteResult:
        """Hand over ``states`` (ascending by time) without raising for a failed hand-over."""


Sleep = Callable[[float], Awaitable[None]]


async def write_throttled(
    writer: StateWriter,
    entity_id: str,
    states: Sequence[StatePoint],
    *,
    block: int = WRITE_BLOCK_SIZE,
    pause_above: int = BACKLOG_PAUSE_ABOVE,
    resume_below: int = BACKLOG_RESUME_BELOW,
    hard_limit: int = BACKLOG_HARD_LIMIT,
    poll_s: float = 0.5,
    max_polls: int = 7200,
    sleep: Sleep = asyncio.sleep,
) -> WriteResult:
    """Write in blocks and let the recorder queue drain in between.

    Above ``pause_above`` queued entries the loop waits until the queue is below ``resume_below``;
    above ``hard_limit`` or after ``max_polls`` waits it stops with an error. The recorder
    switches recording off completely when its queue overflows, so live data would be lost.
    """
    done = 0
    for offset in range(0, len(states), block):
        if writer.backlog > hard_limit:
            return WriteResult(done, "recorder queue is full")
        if writer.backlog > pause_above:
            polls = 0
            while writer.backlog >= resume_below:
                polls += 1
                if polls > max_polls:
                    return WriteResult(done, "recorder queue does not drain")
                await sleep(poll_s)
        result = await writer.write(entity_id, states[offset : offset + block])
        done += result.done
        if result.error:
            return WriteResult(done, result.error)
        await sleep(0)
    return WriteResult(done)
