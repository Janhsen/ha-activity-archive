# Activity Archive for Home Assistant

Archive your sport activities (rides, runs, hikes, swims, ...) as long-term
recorder states in Home Assistant: one activity sensor per person, curve
sensors (heart rate, altitude, speed, distance, cadence, power, temperature)
with one state per measurement point at its real time stamp, and a position
tracker for the GPS track.

> **Status: early development (stage E1).** This release contains the
> parsing and simplification core, the de-duplication rules and the ledger,
> all covered by tests with synthetic data. There is no config flow and no
> Home Assistant writer yet; the integration cannot be installed from HACS
> in a useful way at this stage.

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

## Recorder settings

Curves are written with time stamps in the past. The recorder must keep and
record them:

```yaml
recorder:
  purge_keep_days: 7300        # at least the age of your oldest activity
  include:
    entities:
      - sensor.alex_activity           # example names, one block per person
      - sensor.alex_activity_heart_rate
      - device_tracker.alex_activity_position
```

If your recorder uses `include`/`exclude` filters, every entity the
integration creates has to pass them, otherwise states are dropped
silently. The integration will check this before writing (stage E1b).

## Removing the data

Entity-wide removal is possible with the `recorder.purge_entities` action
(select the activity and curve entities of one person). Deleting a single
time window is not possible through the Home Assistant API. A deleted
activity is remembered as a tombstone in the ledger so it does not return on
the next import.

## Development

```bash
python -m pytest            # tests (pytest)
python -m unittest discover -s tests -t .   # same tests without pytest
ruff check .
python scripts/privacy_guard.py
```

The tests are plain `unittest.TestCase` classes, so both runners work.

## License

MIT, see `LICENSE`.
