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
