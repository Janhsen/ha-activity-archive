# Activity Archive for Home Assistant

Archive your sport activities (rides, runs, hikes, swims, ...) as long-term
recorder states in Home Assistant: one activity sensor per person, curve
sensors (heart rate, altitude, speed, distance, cadence, power, temperature)
with one state per measurement point at its real time stamp, and a position
tracker for the GPS track.

> **Status: early development (stage E1b).** The parsing and simplification
> core (E1) now has its Home Assistant side: config flow, recorder writer,
> ledger storage, services, start checks and diagnostics. The recorder writer
> uses an undocumented recorder interface and has **not yet been run against a
> real Home Assistant** (see "Not yet verified"). The Strava export and Garmin
> adapters follow in later stages; until then you can import single files with
> `activity_archive.import_file`. Do not use it on an installation you cannot
> restore from a backup.

This project is **not affiliated with, endorsed by or sponsored by Strava,
Inc. or Garmin Ltd.** Strava and Garmin are trademarks of their respective
owners.

## What it does

* Reads activity files (FIT, GPX, TCX, also gzip-compressed) with a small
  pure-Python parser. No runtime dependencies.
* Reduces the data to what a chart needs: window means on a fixed grid,
  Douglas-Peucker for the GPS line (maximum deviation 5 m in the "precise"
  profile, plus a time-synchronous limit of 10 m for cycling).
* Never invents values: a channel the source does not contain (for example
  speed in a plain GPX file) is left out, nothing is interpolated or
  calculated from other channels.
* Decides whether two files describe the same activity (start +/-120 s,
  duration +/-5 %, distance +/-3 %); unclear cases keep both.
* Keeps a ledger so that importing the same file twice writes nothing.

## Source rules

1. Per activity, curves come from the densest unchanged source
   (Garmin FIT before Strava export file before summary only).
2. Title, description and sport come from Strava when available.
3. Duplicates are only searched within one person.

## Privacy

* Activity data lives in the recorder database of your Home Assistant.
  Everyone who may see the history panel or a dashboard can see the tracks
  of every person archived. Home Assistant has no per-user entity
  permissions.
* The first and last points of a track reveal where someone lives or works.
* Home Assistant backups contain the recorder database. Encrypt them.
* Never commit or attach activity files, `activities.csv` or exports to
  issues. `.gitignore` and `scripts/privacy_guard.py` (run in CI) block the
  usual file types, coordinates in test data, tokens, e-mail addresses and
  phone numbers.
* Test data in this repository is generated synthetically (fixed seed,
  coordinates near 0 N / 0 E, marker `SYNTHETIC`).

## Set-up

1. Add the integration (Settings > Devices & services > Add integration >
   Activity Archive). Choose the import folder (default
   `/config/activity_archive/import`, created if missing), the detail level
   (Precise, Standard, Coarse) and the first person: display name, a short
   name for entity ids and optionally the Strava athlete name. Further people
   and changes go through the integration's options.
2. Put FIT, GPX or TCX files (also `.gz`) into the import folder.
3. Call `activity_archive.import_file` (Developer tools > Actions). By default
   it is a **dry run**: it only reads the file and returns the number of states
   it would write. Switch `dry_run` off to write.

Entities per person (no live states; they only carry the archived history and
get the label `recorder_longterm`):

| Entity | Content |
| --- | --- |
| `sensor.activity_archive_<person>` | one state per activity at its start, device totals as attributes |
| `sensor.activity_archive_<person>_heart_rate`, `_altitude`, `_speed`, `_distance`, `_cadence`, `_power`, `_temperature` | curves, one state per measurement point (only channels the file really contains) |
| `device_tracker.activity_archive_<person>_position` | the simplified GPS track |

## Services

All services are administrator-only and return a result.

* `activity_archive.import_file`: `file` (name inside the import folder),
  `person` (optional with one person), `dry_run` (default on).
* `activity_archive.verify`: compares the ledger with the recorder and lists
  completed imports whose states are missing (`run_id` limits it to one run).
* `activity_archive.rollback`: `run_id`, `confirm`, `purge_entities`. Without
  `confirm` it only reports. With it the run's activities are tombstoned so
  they are not imported again. The states stay in the recorder unless
  `purge_entities` is set; then **all** states of entities that only this run
  used are purged (never entities another run used).
* `activity_archive.write_test_state` and `activity_archive.remove_test_state`:
  the E0 test. It writes exactly one state stamped 2017-06-15 12:00 UTC to
  `sensor.activity_archive_test` (label `recorder_longterm`). Look at its
  history after your next night purge: it is still there only if retention,
  recorder filter and label protect it as intended. Removing purges only that
  entity.

An import never runs twice at the same time. If Home Assistant stops in the
middle of a file, call the service again with the same file: the recorder is
asked which states exist and only the missing ones are written.

## Recorder settings

Curves are written with time stamps in the past. The recorder must keep and
record them. The integration checks this at start-up and shows repair issues
(it never changes your configuration):

```yaml
recorder:
  purge_keep_days: 7300        # at least the age of your oldest activity
  include:
    entity_globs:
      - sensor.activity_archive_*
      - device_tracker.activity_archive_*
```

If your recorder uses `include`/`exclude` filters, every entity the
integration creates has to pass them, otherwise states are dropped
silently. The writer refuses to write while an entity is filtered out or while
`purge_keep_days` is below 7300 (with auto purge on). Also check automations
that call `recorder.purge` or `recorder.purge_entities`: they know nothing
about the archive, and the label `recorder_longterm` only protects you if your
own purge logic honours it.

Other start checks (hints only): recorder not recording, low free disk space,
and a database other than PostgreSQL (large imports are untested there).

## Removing the data

Entity-wide removal is possible with the `recorder.purge_entities` action
(select the activity and curve entities of one person). Deleting a single
time window is not possible through the Home Assistant API. A deleted
activity is remembered as a tombstone in the ledger (`rollback`) so it does
not return on the next import.

## Not yet verified

* The recorder write path (`Recorder.queue_task` with hand-built
  `state_changed` events) is the technique of the `garmin_personal`
  integration, but this repository's code was only tested against fakes of
  Home Assistant, not against a real recorder.
* Entity registry entries without platform code, the label registry calls, the
  `recorder.purge_entities` call and the config/options flow have likewise
  only run against fakes.

## Development

```bash
python -m pytest            # tests (pytest)
python -m unittest discover -s tests -t .   # same tests without pytest
ruff check .
python scripts/privacy_guard.py
```

Home Assistant is not needed for the tests: `tests/ha_stubs.py` provides small
fakes of the parts of Home Assistant that are used (and steps aside when a real
Home Assistant is installed).

The tests are plain `unittest.TestCase` classes, so both runners work.

## License

MIT, see `LICENSE`.
