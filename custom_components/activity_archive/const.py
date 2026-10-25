"""Constants of the Activity Archive integration."""

from __future__ import annotations

DOMAIN = "activity_archive"

# Bump when the layout of ledger data or activity attributes changes.
SCHEMA_VERSION = 1

# Largest attribute payload the integration writes. The recorder replaces attributes above
# 16384 bytes by ``{}``, so the activity sensor stays well below.
MAX_ATTRIBUTES_BYTES = 12 * 1024
MAX_STATE_LENGTH = 255

# Marker states for "no value here": after the last point and after gaps.
MARKER_STATE = "unavailable"
TRACKER_STATE = "not_home"

# Backlog brake for writing into the recorder queue (see core/writer.py).
WRITE_BLOCK_SIZE = 500
BACKLOG_PAUSE_ABOVE = 3000
BACKLOG_RESUME_BELOW = 1000
BACKLOG_HARD_LIMIT = 5000

# Recorder retention the integration insists on before it writes anything.
MIN_PURGE_KEEP_DAYS = 7300

# Duplicate detection thresholds (decided by the owner, see README).
DEDUPE_START_TOLERANCE_S = 120
DEDUPE_DURATION_TOLERANCE = 0.05
DEDUPE_DISTANCE_TOLERANCE = 0.03

# Activities shorter than this are reported instead of written (option, default on).
MIN_DURATION_S = 60

PROFILE_PRECISE = "precise"
PROFILE_STANDARD = "standard"
PROFILE_COARSE = "coarse"
DEFAULT_PROFILE = PROFILE_PRECISE

# ---------------------------------------------------------------------------------------------
# Home Assistant layer (stage E1b)
# ---------------------------------------------------------------------------------------------
CONF_IMPORT_DIR = "import_dir"
CONF_PROFILE = "profile"
CONF_PEOPLE = "people"
CONF_PERSON_NAME = "name"
CONF_PERSON_SLUG = "slug"
CONF_PERSON_ATHLETE = "athlete_name"

DEFAULT_IMPORT_DIR = "/config/activity_archive/import"
PROFILES = (PROFILE_PRECISE, PROFILE_STANDARD, PROFILE_COARSE)

# Label that protects entities from the "delete everything without this label" night purge.
LABEL_LONGTERM = "recorder_longterm"

# E0 test: exactly one state at a fixed time in the past on an entity of its own.
TEST_OBJECT_ID = "activity_archive_test"
TEST_ENTITY_ID = f"sensor.{TEST_OBJECT_ID}"
TEST_TIMESTAMP = 1497528000  # 2017-06-15 12:00:00 UTC
TEST_STATE = "test"

# Source name in the ledger for a file imported by hand (``activity_archive.import_file``).
SOURCE_FILE = "file"
IMPORT_SUFFIXES = (".fit", ".gpx", ".tcx", ".gz")

# Rough free space the start check wants on the volume of the recorder database.
MIN_FREE_BYTES = 5 * 2**30

LEDGER_STORE_VERSION = 1
LEDGER_STORE_KEY = f"{DOMAIN}.ledger"

# Padding (seconds) around an activity when ledger and recorder are compared.
VERIFY_PAD_S = 5
