"""Start checks as pure functions: facts about recorder and disk in, findings out.

The Home Assistant layer collects the facts (``issues.py``) and turns the findings into repair
issues. Nothing here changes any setting; the checks only read and advise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from ..const import MIN_FREE_BYTES, MIN_PURGE_KEEP_DAYS

ISSUE_RETENTION: Final = "retention_too_short"
ISSUE_FILTER: Final = "not_recorded"
ISSUE_BACKEND: Final = "database_backend"
ISSUE_DISK: Final = "low_disk_space"
ISSUE_RECORDER_OFF: Final = "recorder_not_recording"
ISSUE_LEDGER: Final = "ledger_unreadable"
ALL_ISSUES: Final = (
    ISSUE_RETENTION,
    ISSUE_FILTER,
    ISSUE_BACKEND,
    ISSUE_DISK,
    ISSUE_RECORDER_OFF,
    ISSUE_LEDGER,
)

WARNING: Final = "warning"
ERROR: Final = "error"

# Backends the integration was designed for. Others work in principle but are untested.
TESTED_DIALECTS: Final = frozenset({"postgresql"})


@dataclass(frozen=True, slots=True)
class HealthFacts:
    """What was read from the recorder and the disk (``None``: could not be determined)."""

    keep_days: int | None = None
    auto_purge: bool = True
    dialect: str | None = None
    free_bytes: int | None = None
    recording: bool = True
    unrecorded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    """One advice; ``key`` doubles as issue id and translation key."""

    key: str
    severity: str
    placeholders: dict[str, str] = field(default_factory=dict)


def evaluate(facts: HealthFacts) -> list[Finding]:
    """Return the findings for ``facts``, most important first."""
    out: list[Finding] = []
    if not facts.recording:
        out.append(Finding(ISSUE_RECORDER_OFF, ERROR))
    if facts.auto_purge and facts.keep_days is not None and facts.keep_days < MIN_PURGE_KEEP_DAYS:
        out.append(
            Finding(
                ISSUE_RETENTION,
                ERROR,
                {"keep_days": str(facts.keep_days), "needed": str(MIN_PURGE_KEEP_DAYS)},
            )
        )
    if facts.unrecorded:
        shown = ", ".join(facts.unrecorded[:4])
        more = len(facts.unrecorded) - 4
        out.append(
            Finding(
                ISSUE_FILTER,
                ERROR,
                {
                    "entities": shown + (f" (+{more})" if more > 0 else ""),
                    "count": str(len(facts.unrecorded)),
                },
            )
        )
    if facts.dialect and facts.dialect not in TESTED_DIALECTS:
        out.append(Finding(ISSUE_BACKEND, WARNING, {"backend": facts.dialect}))
    if facts.free_bytes is not None and facts.free_bytes < MIN_FREE_BYTES:
        out.append(
            Finding(
                ISSUE_DISK,
                WARNING,
                {
                    "free_gb": f"{facts.free_bytes / 2**30:.1f}",
                    "needed_gb": f"{MIN_FREE_BYTES / 2**30:.0f}",
                },
            )
        )
    return out
