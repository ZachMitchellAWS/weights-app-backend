"""Effort classification and local-time resolution for session generation.

This module deliberately does NOT reuse `insights/lambda/utils/data_curator._effort_tier`.
That classifier has four buckets and collapses everything at or above 82% into "Hard", so it
never produces a near-max result. The iOS app's `TrendsCalculator.IntensityBucket` has five,
splitting again at 92%.

The payload here is compared directly against what the user sees in the Lift tab, so it must
match the app exactly. Insights is left as it is on purpose — its output is prose, where the
distinction does not matter, and changing it would alter a live feature. If you are here to
make the two agree, read that paragraph again first.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

# Payload-facing effort keys. These are NOT the app's storage keys: it persists "redline" for
# near-max and "pr" for progress. The payload uses the words the product says out loud.
EASY = "easy"
MODERATE = "moderate"
HARD = "hard"
NEAR_MAX = "near_max"
PROGRESS = "progress"

# Ratio floors, from TrendsCalculator.IntensityBucket.from(percent1RM:).
# Keep in step with the client — a change there is a change here.
NEAR_MAX_FLOOR = 0.92
HARD_FLOOR = 0.82
MODERATE_FLOOR = 0.70


def to_float(value) -> float:
    """DynamoDB returns numbers as Decimal."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def epley_e1rm(weight: float, reps: int) -> float:
    """e1RM = weight x (1 + reps / 30). Matches OneRMCalculator on the client."""
    if reps <= 0:
        return weight
    return weight * (1 + reps / 30)


def classify(weight: float, reps: int, e1rm_before: float, e1rm_after: float) -> str | None:
    """Effort key for one set, mirroring `CheckInView.actualEffort`.

    `e1rm_before` is the running-max e1RM standing immediately BEFORE this set;
    `e1rm_after` is the row attached to this set. Returns None for a set the app itself
    declines to classify.

    An Estimated1RM row is written for every set and its value is a running max, so the
    series is monotonically non-decreasing per exercise. That is why "most recent row before
    this set" and "max of rows before this set" are the same number, and why `e1rm_after`
    is a safe fallback when nothing precedes the set.
    """
    if weight <= 0:
        # The app shows "Logged" with no effort for zero-weight sets. There is no honest key
        # for that, so the caller omits the set rather than inventing one.
        return None

    estimated = epley_e1rm(weight, reps)

    prior = e1rm_before
    if prior <= 0:
        # First-ever set for this exercise: the app falls back to the set's own e1RM row,
        # which makes the ratio exactly 1.0 and lands it in near-max rather than progress.
        prior = e1rm_after

    if prior <= 0:
        return None

    if estimated > prior + 1e-9:
        return PROGRESS

    ratio = estimated / prior
    if ratio >= NEAR_MAX_FLOOR:
        return NEAR_MAX
    if ratio >= HARD_FLOOR:
        return HARD
    if ratio >= MODERATE_FLOOR:
        return MODERATE
    return EASY


def definitions() -> dict:
    """The effort keys explained, for the payload's `effort_level_definitions`.

    Built from the floors above rather than written out, so the numbers the model reasons
    with are literally the numbers `classify` used. The system prompt explains what the keys
    mean but deliberately does not restate the bounds — there is one source of truth for them
    and it is this module.

    Bounds are percent of the estimated 1RM standing at the time; min inclusive, max exclusive.
    """
    def pct(ratio: float) -> int:
        return int(round(ratio * 100))

    return {
        EASY: {
            "min_percent_1rm": 0,
            "max_percent_1rm": pct(MODERATE_FLOOR),
            "description": "Well below working weight. Warm-up or recovery volume.",
        },
        MODERATE: {
            "min_percent_1rm": pct(MODERATE_FLOOR),
            "max_percent_1rm": pct(HARD_FLOOR),
            "description": "Working weight. Repeatable without approaching failure.",
        },
        HARD: {
            "min_percent_1rm": pct(HARD_FLOOR),
            "max_percent_1rm": pct(NEAR_MAX_FLOOR),
            "description": "Demanding. Close to but short of a maximal effort.",
        },
        NEAR_MAX: {
            "min_percent_1rm": pct(NEAR_MAX_FLOOR),
            "max_percent_1rm": None,
            "description": "At or near the current ceiling, without exceeding it.",
        },
        PROGRESS: {
            "min_percent_1rm": None,
            "max_percent_1rm": None,
            "description": (
                "NOT an intensity band. In recent_training: the set exceeded the standing "
                "estimated 1RM, so the ceiling moved. In a set plan sequence: attempt to "
                "exceed it."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Local time
# ---------------------------------------------------------------------------


def offset_seconds(row: dict) -> int:
    """UTC offset for a record, in seconds east of UTC.

    Two stages, in order:
      1. `createdUtcOffsetSeconds` — captured on the device at write time, so it reflects the
         tz rules in force at that moment. Exact.
      2. Resolve `createdTimezone` against the record's own instant. Needs `tzdata` in the
         layer, and depends on whichever tzdata version was bundled at the last build.

    `is not None`, never truthiness: 0 is a legal offset (UTC, London in winter).
    """
    stored = row.get("createdUtcOffsetSeconds")
    if stored is not None:
        return int(to_float(stored))

    created = row.get("createdDatetime")
    if not created:
        return 0

    try:
        tz = ZoneInfo(row.get("createdTimezone") or "UTC")
    except Exception:
        return 0

    dt = parse_iso(created)
    if dt is None:
        return 0
    delta = dt.astimezone(tz).utcoffset()
    return int(delta.total_seconds()) if delta else 0


def parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 string, tolerating a trailing Z and assuming UTC when naive."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_datetime(row: dict) -> datetime | None:
    """The record's instant, expressed in its own local offset.

    Used for both the date bucket and the `at` field, so a set's timestamp can never
    contradict the date key it is filed under.
    """
    dt = parse_iso(row.get("createdDatetime", ""))
    if dt is None:
        return None
    return dt.astimezone(timezone(timedelta(seconds=offset_seconds(row))))
