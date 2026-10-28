# Working rules for Claude – ha-activity-archive

Public Home Assistant custom integration (HACS), domain `activity_archive`. It archives sport activities (curves, GPS track) as recorder states. Everything in this repository is public.

## Public-repo rule (always)
- Never write personal data into files, commits, issues or test data: no real names, no places or addresses, no employer, no phone numbers, e-mail addresses, IDs or tokens. Use invented examples.
- Never commit activity files or exports (`.fit`, `.gpx`, `.tcx`, archives, databases, `activities.csv`). Test data is generated synthetically (fixed seed, coordinates near 0 N / 0 E, marker `SYNTHETIC`).
- Do not describe the owner's private setup. Keep text technical.
- `scripts/privacy_guard.py` runs in CI (including history). Run it locally before every push: `python scripts/privacy_guard.py --history`.

## Layout
- `custom_components/activity_archive/core/`: pure logic without Home Assistant (readers `fit_reader.py`, `gpx_reader.py`, `resample.py`, `simplify.py`, `dedupe.py`, `ledger.py`, `importer.py`, `writer.py`, `sports.py`).
- `custom_components/activity_archive/` (top level): Home Assistant side (`config_flow.py`, `manager.py`, `recorder_writer.py`, `ledger_store.py`, `services.py`, `entities.py`, `issues.py`, `diagnostics.py`).
- `adapters/`: source adapters (base class only so far; see the README status for what exists).
- `tests/`: unit and Home Assistant stub tests.
- The README status section says what is implemented and what is not yet verified against a real Home Assistant. Keep it accurate; do not claim more than was tested.

## Rules for the code
- Never invent values. A channel the source does not contain is left out; nothing is interpolated or calculated from other channels.
- No runtime dependencies (`requirements: []`). Parsers are pure Python.
- Source rules: curves come from the densest unchanged source (FIT before Strava export file before summary only); title, description and sport come from Strava when available; duplicates are only searched within one person (start ±120 s, duration ±5 %, distance ±3 %; unclear cases keep both).
- The recorder writer uses an undocumented recorder interface. Change it carefully and keep the start-up checks and repair issues working. The integration never changes the user's configuration.
- Imports must be repeatable: importing the same file twice writes nothing (ledger).

## Commands
- Lint: `ruff check .` and `ruff format --check .`
- Tests: `python -m pytest -q`
- CI also runs `hassfest` and the HACS action (`.github/workflows/validate.yml`). Translation strings must not contain angle brackets (hassfest).

## Workflow
- Docs in English. Translations in `translations/en.json` and `de.json`, source `strings.json`.
- Version lives in `manifest.json` and `pyproject.toml`; keep both equal.
- Ask the maintainer whether a change goes to a branch or directly to `main`.
- Trademarks: not affiliated with Strava or Garmin; keep the disclaimer in the README.
