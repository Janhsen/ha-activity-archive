"""Start checks: read recorder settings and disk space, advise through repair issues.

Only reads. Nothing in the recorder configuration or the system is changed.
"""

from __future__ import annotations

import shutil
from typing import Any

from homeassistant.components import recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .core.health import ALL_ISSUES, ERROR, Finding, HealthFacts, evaluate

LEARN_MORE = "https://github.com/Janhsen/ha-activity-archive#recorder-settings"


async def async_collect_facts(hass: HomeAssistant, entity_ids: list[str]) -> HealthFacts:
    """Read what the checks need; every value that cannot be read stays ``None``."""
    keep_days: int | None = None
    auto_purge = True
    dialect: str | None = None
    recording = True
    unrecorded: list[str] = []
    try:
        instance = recorder.get_instance(hass)
    except Exception:  # recorder not available
        instance = None
    if instance is not None:
        value = getattr(instance, "keep_days", None)
        keep_days = value if isinstance(value, int) else None
        auto_purge = bool(getattr(instance, "auto_purge", True))
        name = getattr(instance, "dialect_name", None)
        dialect = str(name) if name is not None else None
        recording = bool(getattr(instance, "recording", True))
        entity_filter = getattr(instance, "entity_filter", None)
        if entity_filter is not None:
            unrecorded = [e for e in entity_ids if not entity_filter(e)]
    try:
        free: int | None = (
            await hass.async_add_executor_job(shutil.disk_usage, hass.config.config_dir)
        ).free
    except OSError:
        free = None
    return HealthFacts(
        keep_days=keep_days,
        auto_purge=auto_purge,
        dialect=dialect,
        free_bytes=free,
        recording=recording,
        unrecorded=tuple(unrecorded),
    )


def sync_issues(hass: HomeAssistant, findings: list[Finding]) -> list[str]:
    """Create the issues of ``findings`` and delete all others of this integration.

    The ledger issue is not touched here (it is raised and cleared by the manager).
    """
    wanted = {f.key: f for f in findings}
    for key in ALL_ISSUES:
        if key == "ledger_unreadable":
            continue
        finding = wanted.get(key)
        if finding is None:
            ir.async_delete_issue(hass, DOMAIN, key)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR
            if finding.severity == ERROR
            else ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=dict(finding.placeholders),
            learn_more_url=LEARN_MORE,
        )
    return sorted(wanted)


async def async_run_checks(hass: HomeAssistant, entity_ids: list[str]) -> list[Finding]:
    """Collect facts, evaluate them and update the repair issues."""
    findings = evaluate(await async_collect_facts(hass, entity_ids))
    sync_issues(hass, findings)
    return findings


def set_ledger_issue(hass: HomeAssistant, found_version: Any) -> None:
    """Raise the repair issue for a ledger file this version cannot read."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "ledger_unreadable",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="ledger_unreadable",
        translation_placeholders={"version": str(found_version)},
    )


def clear_ledger_issue(hass: HomeAssistant) -> None:
    """Remove the ledger issue."""
    ir.async_delete_issue(hass, DOMAIN, "ledger_unreadable")
