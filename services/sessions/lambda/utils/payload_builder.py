"""Assemble the model payload from DynamoDB.

Shape is specified in `SESSION_GENERATION_INPUTS.md` at the repo root. Read that first —
this module implements it, it does not define it.

The client sends only what the backend cannot know (the set plan catalog and the user's
free-text context). Everything else is queried here.
"""

import logging
import os
import statistics
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

from utils import effort

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")

# Canonical order, matching TrendsCalculator.fundamentalExercises. Never Exercise.builtInIds,
# which orders Overhead Press before Barbell Rows.
CORE_EXERCISES = ["Deadlifts", "Squats", "Bench Press", "Barbell Rows", "Overhead Press"]

TIER_ORDER = ["Novice", "Beginner", "Intermediate", "Advanced", "Elite", "Legend"]

# Minimum bodyweight multiplier to enter each tier, ordered Novice -> Legend.
TIER_THRESHOLDS: dict[str, dict[str, list[float]]] = {
    "Deadlifts":      {"male": [0, 1.0, 1.5, 2.25, 3.0, 3.5],
                       "female": [0, 0.5, 1.0, 1.75, 2.25, 3.0]},
    "Squats":         {"male": [0, 0.75, 1.25, 1.75, 2.5, 3.0],
                       "female": [0, 0.5, 1.0, 1.5, 1.75, 2.25]},
    "Bench Press":    {"male": [0, 0.5, 1.0, 1.5, 2.0, 2.25],
                       "female": [0, 0.25, 0.5, 0.75, 1.0, 1.25]},
    "Barbell Rows":   {"male": [0, 0.50, 0.75, 1.0, 1.5, 1.75],
                       "female": [0, 0.25, 0.40, 0.65, 0.90, 1.20]},
    "Overhead Press": {"male": [0, 0.40, 0.55, 0.80, 1.05, 1.35],
                       "female": [0, 0.20, 0.35, 0.55, 0.75, 1.00]},
}

WINDOW_DAYS = 30

# Indexed by `date.weekday()` (Monday = 0). Hardcoded rather than `strftime("%A")`, which
# is locale-dependent — the Lambda's locale is not something this code should depend on.
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
# How far past the window we will walk backwards looking for e1RM baselines before giving up.
BASELINE_PAGE_CAP = 6


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _table(env_var: str):
    name = os.environ.get(env_var)
    if not name:
        raise ValueError(f"{env_var} environment variable not set")
    return dynamodb.Table(name)


def _query_all(table, **kwargs) -> list[dict]:
    """Run a query to exhaustion."""
    items: list[dict] = []
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def query_exercises(user_id: str) -> list[dict]:
    return _query_all(_table("EXERCISES_TABLE_NAME"),
                      KeyConditionExpression=Key("userId").eq(user_id))


def query_user_properties(user_id: str) -> dict:
    table = _table("USER_PROPERTIES_TABLE_NAME")
    response = table.get_item(Key={"userId": user_id})
    return response.get("Item") or {}


def query_lift_sets(user_id: str, start_iso: str, end_iso: str) -> list[dict]:
    return _query_all(
        _table("LIFT_SETS_TABLE_NAME"),
        IndexName="userId-createdDatetime-index",
        KeyConditionExpression=(
            Key("userId").eq(user_id) & Key("createdDatetime").between(start_iso, end_iso)
        ),
    )


def query_estimated_1rm(user_id: str, start_iso: str, end_iso: str,
                        exercise_ids: set[str]) -> list[dict]:
    """Window rows, plus just enough older rows to establish a baseline per exercise.

    Classifying the first set of the window needs the e1RM standing before it, which by
    definition predates the window. There is no per-exercise index on this table (PK userId,
    SK liftSetId, GSI on createdDatetime), so a targeted lookup is not available.

    Insights answers this with an unbounded full-history query — one row per set ever logged,
    thousands of items for a long-time user. Instead this walks backwards from the window
    start and stops as soon as every exercise has a baseline. For anyone training regularly
    that is a page or two; a dormant lift costs a few more, capped.
    """
    table = _table("ESTIMATED_1RM_TABLE_NAME")

    window = _query_all(
        table,
        IndexName="userId-createdDatetime-index",
        KeyConditionExpression=(
            Key("userId").eq(user_id) & Key("createdDatetime").between(start_iso, end_iso)
        ),
    )

    needed = set(exercise_ids)
    for row in window:
        if not row.get("deleted"):
            needed.discard(row.get("exerciseId"))

    baselines: list[dict] = []
    if needed:
        kwargs = {
            "IndexName": "userId-createdDatetime-index",
            "KeyConditionExpression": (
                Key("userId").eq(user_id) & Key("createdDatetime").lt(start_iso)
            ),
            "ScanIndexForward": False,   # newest-first, so the first hit per exercise wins
        }
        for _ in range(BASELINE_PAGE_CAP):
            response = table.query(**kwargs)
            for row in response.get("Items", []):
                if row.get("deleted"):
                    continue
                ex_id = row.get("exerciseId")
                if ex_id in needed:
                    baselines.append(row)
                    needed.discard(ex_id)
            if not needed:
                break
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last

    return window + baselines


# ---------------------------------------------------------------------------
# Strength tier
# ---------------------------------------------------------------------------


def _tier_index(exercise_name: str, e1rm: float, bodyweight: float, sex: str) -> int:
    thresholds = TIER_THRESHOLDS.get(exercise_name, {}).get(sex)
    if not thresholds or bodyweight <= 0:
        return 0
    multiplier = e1rm / bodyweight
    index = 0
    for i, floor in enumerate(thresholds):
        if multiplier >= floor:
            index = i
    return index


def _tier_progress(exercise_name: str, e1rm: float, bodyweight: float, sex: str) -> float:
    """Fraction from the current tier's floor to the next tier's floor, 0-1.

    Legend has no ceiling, so it always reports 1.0.
    """
    thresholds = TIER_THRESHOLDS.get(exercise_name, {}).get(sex)
    if not thresholds or bodyweight <= 0:
        return 0.0
    index = _tier_index(exercise_name, e1rm, bodyweight, sex)
    if index >= len(thresholds) - 1:
        return 1.0
    floor = thresholds[index] * bodyweight
    ceiling = thresholds[index + 1] * bodyweight
    if ceiling <= floor:
        return 1.0
    return max(0.0, min(1.0, (e1rm - floor) / (ceiling - floor)))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_payload(user_id: str, set_plan_catalog: list[dict], user_context: dict) -> dict:
    """Assemble the full model payload. Returns the dict described in the reference doc."""
    props = query_user_properties(user_id)
    bodyweight = effort.to_float(props.get("bodyweight"))
    sex = (props.get("biologicalSex") or "male").lower()
    weight_unit = props.get("weightUnit") or "lbs"

    # THE LIFT FILTER, AND THE ENFORCEMENT.
    #
    # Lifts the user switched off are REMOVED from the payload rather than described to the
    # model as forbidden. `sessions.generate` builds its allow-list of valid exercise ids
    # straight from `strength.lifts` below, and `_validate_response` already rejects a
    # response naming an id that was not sent — so an excluded lift has no id for the model
    # to return, and inventing one fails the check that already exists.
    #
    # A prompt rule would have been one more instruction competing with ten others. This is
    # not a rule; it is an absence.
    #
    # `recent_training` deliberately KEEPS every lift, including excluded ones. It is history,
    # and the summary is better for it — "you squatted heavy Tuesday, so today is upper" needs
    # to know about Tuesday. Selection is gated on `strength.lifts`, never on history, so
    # showing it costs nothing.
    excluded = {n for n in (user_context.get("excluded_lifts") or []) if n in CORE_EXERCISES}
    included = [n for n in CORE_EXERCISES if n not in excluded]

    exercises = [e for e in query_exercises(user_id) if not e.get("deleted")]
    by_name = {e.get("name"): e for e in exercises if e.get("name") in CORE_EXERCISES}
    id_by_name = {n: by_name[n]["exerciseItemId"] for n in by_name}
    name_by_id = {v: k for k, v in id_by_name.items()}

    # The user's own local day governs the window and the bucketing, matching how the app
    # bins Today's Sets. Derived from the most recent record rather than the server clock.
    today_local, tz_name = _today_local(props)
    window_start = today_local - timedelta(days=WINDOW_DAYS - 1)
    start_iso = (window_start - timedelta(days=2)).isoformat()   # pad for offset skew
    end_iso = (today_local + timedelta(days=2)).isoformat()

    lift_sets = [s for s in query_lift_sets(user_id, start_iso, end_iso) if not s.get("deleted")]
    e1rm_rows = [r for r in query_estimated_1rm(user_id, start_iso, end_iso, set(id_by_name.values()))
                 if not r.get("deleted")]

    e1rm_by_set = {r.get("liftSetId"): effort.to_float(r.get("value")) for r in e1rm_rows}
    e1rm_series = _series_by_exercise(e1rm_rows)

    recent_training = _build_recent_training(
        lift_sets, name_by_id, e1rm_by_set, e1rm_series, window_start, today_local
    )

    lifts = {}
    for name in included:
        if name not in id_by_name:
            continue
        current = _current_e1rm(e1rm_series.get(id_by_name[name], []))
        lifts[name] = {
            "id": id_by_name[name],
            "current_e1rm": round(current, 1),
            "tier": TIER_ORDER[_tier_index(name, current, bodyweight, sex)],
            "tier_progress": round(_tier_progress(name, current, bodyweight, sex), 2),
            "days_since_last_trained": _days_since_last_trained(
                recent_training, name, today_local
            ),
            "progress_readiness": _progress_readiness(
                recent_training, name, current, today_local
            ),
        }

    overall = min((TIER_ORDER.index(l["tier"]) for l in lifts.values()), default=0)

    return {
        "generated_at": datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account_created": _account_age(props, today_local),
        "local_date": today_local.isoformat(),
        "timezone": tz_name,
        "weight_unit": weight_unit,
        "strength": {
            "overall_tier": TIER_ORDER[overall],
            "lifts": lifts,
        },
        "recent_training": recent_training,
        "rotation_order": _rotation_order(recent_training, lifts, today_local),
        "today_coverage": _today_coverage(recent_training, today_local, included),
        "date_labels": _date_labels(window_start, today_local),
        "effort_level_definitions": effort.definitions(),
        "set_plan_catalog": set_plan_catalog,
        "user_context": user_context,
    }


def _today_local(props: dict) -> tuple[date, str]:
    """The user's current local date, and the timezone it was resolved from.

    The server's own clock is never the answer — a 9pm session in California is the next day
    in UTC, and bucketing it there would disagree with the app.

    Note the priority is the REVERSE of the per-record rule in `effort.offset_seconds`. On a
    record, `createdUtcOffsetSeconds` was captured at that instant and is exact. On
    user-properties, `utcOffsetSeconds` is a cache of whatever the device last reported and
    does not move when DST does, so `timezone` is authoritative for a date being computed now.
    """
    now = datetime.now(dt_timezone.utc)
    tz_name = props.get("timezone")

    if tz_name:
        try:
            return now.astimezone(ZoneInfo(tz_name)).date(), tz_name
        except Exception:
            logger.warning("Unresolvable user timezone %r, falling back", tz_name)

    cached = props.get("utcOffsetSeconds")
    if cached is not None:
        offset = int(effort.to_float(cached))
        return (now.astimezone(dt_timezone(timedelta(seconds=offset))).date(),
                f"UTC{offset // 3600:+d}")

    return now.date(), "UTC"


# A lift counts as done for the day AT OR ABOVE this many sets. Not "any sets": one
# baseline set leaves a lift very much still trainable.
#
# Was 6, on the reasoning that six sets is a session's worth of work on one lift. That was
# the wrong unit. A generated session prescribes 3-5 sets PER LIFT across several lifts, so
# a user who completed every set of their squat item still sat at 3 or 4 — under the line —
# and the next generation of the day recommended squats again. Asking someone to redo work
# they just finished is the most visible way this feature can look broken.
#
# Inclusive: the third set is the one that finishes the lift, not the one after.
#
# The trade this accepts: three genuinely light sets now cover a lift that arguably still
# has work in it. That is the better error. Under-recommending a lift the user already
# touched today costs them one lift out of five; over-recommending work they just completed
# makes the whole session look like it did not read their log.
COVERED_SET_COUNT = 3


# --- Progress readiness ---------------------------------------------------
#
# The app records the OUTCOME of a set, never the intent, so there is no such thing as a
# stored "failed progress attempt". What one looks like in the data is a near-max set: the
# lifter went to the ceiling and did not pass it. One of those is a hard day. Several with
# no progress set among them is a lift that keeps being asked a question it cannot yet
# answer, and the answer is volume, not another attempt.
STALLED_NEAR_MAX_COUNT = 3

# A progress set moves the ceiling by *some* amount, but the amount carries the meaning. Under
# a pound is the ceiling technically moving and practically standing still.
#
# ABSOLUTE, not a fraction of the lift. This was 1% of current e1RM, which sounds more
# principled and behaves worse: it demanded a 3lb jump on a 300lb deadlift and let a 0.9lb
# jump on an overhead press pass, so the same physical progress read as healthy on one lift
# and stalled on another. A pound is a pound — it is also roughly the smallest change the
# plate math can express, which is what makes it the honest floor.
MIN_MEANINGFUL_INCREMENT_LBS = 1.0

# Progress this long ago stops counting as recent, whatever it was worth at the time.
PROGRESS_DUE_DAYS = 10

# Below this many sets in the whole window there is nothing to read, and a confident verdict
# would be invented rather than observed.
MIN_SETS_FOR_READINESS = 3


def _account_age(props: dict, today_local: date) -> str:
    """How long this account has existed, in broad buckets.

    Without it the model reads an empty 30-day window as a lapse and writes things like
    "today is effectively a fresh start — there is no real training history". For someone
    who signed up an hour ago that is not a gentle observation, it is wrong: the history is
    not missing, it could not exist yet.

    Deliberately a bucket, not a date. The model needs to know which story it is telling —
    brand new, or returning after a gap — and an exact timestamp only invites it to do
    arithmetic and quote a number nobody asked for.
    """
    created = effort.parse_iso(props.get("createdDatetime", ""))
    if created is None:
        return "unknown"

    days = (today_local - created.date()).days
    if days <= 0:
        return "today"
    if days < 7:
        return "this_week"
    if days < 31:
        return "this_month"
    return "over_a_month"


def _today_coverage(recent_training: dict, today_local: date, names: list[str] | None = None) -> dict:
    """Per-lift: is there genuinely nothing left to do on this lift today?

    Computed here rather than inferred by the model, because "already trained today" was
    being read far too loosely — a single baseline set from the strength-tier journey made
    a lift look finished, which made the whole day look finished, and the user got
    "you're covered for today" after logging five calibration sets.

    A lift is covered when EITHER:
      * `COVERED_SET_COUNT` or more sets were logged today — real volume, whatever the
        efforts were; or
      * a NON-BASELINE progress set landed today — the ceiling moved, which is the point
        of a session and the natural place to stop.

    Baseline sets are excluded from the progress test on purpose: the first ever set for
    an exercise classifies as `progress` almost by construction, so counting it would make
    a brand-new user's very first session read as complete. They still count toward the
    set total, because a baseline set is work that was actually performed.
    """
    today_key = today_local.isoformat()
    today = recent_training.get(today_key, {})

    coverage = {}
    # Only the lifts still in play. `all_covered` then answers "is there anything left among
    # the lifts they ALLOWED", which is the right question — a user who excluded three lifts
    # and finished the other two is genuinely done for today.
    for name in (names if names is not None else CORE_EXERCISES):
        sets_today = today.get(name, [])
        progress_today = any(
            s.get("effort") == effort.PROGRESS and not s.get("baseline")
            for s in sets_today
        )
        coverage[name] = {
            "covered": len(sets_today) >= COVERED_SET_COUNT or progress_today,
            "sets_today": len(sets_today),
            "progress_set_today": progress_today,
        }
    return coverage


def all_covered(coverage: dict) -> bool:
    """True only when every fundamental is done for the day."""
    return bool(coverage) and all(lift["covered"] for lift in coverage.values())


def _days_since(day_key: str | None, today_local: date) -> int | None:
    """Whole days from an ISO date key to the user's today. None passes through."""
    if not day_key:
        return None
    return (today_local - date.fromisoformat(day_key)).days


def _days_since_last_trained(recent_training: dict, name: str, today_local: date) -> int | None:
    """Days since this lift last had ANY set logged. None when the window holds none.

    Baselines count. They are calibration rather than training, and `progress_readiness`
    excludes them for that reason — but this answers "when did you last touch this lift",
    and the user who logged one did touch it.
    """
    days = [d for d in sorted(recent_training) if recent_training[d].get(name)]
    return _days_since(days[-1] if days else None, today_local)


def _rotation_order(recent_training: dict, lifts: dict, today_local: date) -> list[str]:
    """The five lifts, longest-untrained first. The session's default running order.

    Computed here because it is the spine of the whole selection procedure and the model was
    not reliably deriving it. Left to judgement it kept returning the same two or three lifts
    every day — the ones with the most history to reason about — while the lift nobody had
    touched in three weeks stayed untouched. An explicit order makes rotation the default and
    a deviation something the model has to actively choose.

    Never-trained-in-window sorts first: an empty 30 days is the longest gap there is.

    Ties break on `tier_progress` ascending, so the lift furthest behind its peers goes first.
    That was previously a separate rule the model applied inconsistently; folding it in here
    makes it deterministic and removes a step from the prompt. Final tiebreak is the canonical
    lift order, purely so the result is stable.
    """
    NEVER = 10 ** 6

    def key(name: str):
        days = _days_since_last_trained(recent_training, name, today_local)
        return (
            -(days if days is not None else NEVER),
            lifts.get(name, {}).get("tier_progress", 0.0),
            CORE_EXERCISES.index(name),
        )

    return sorted(lifts.keys(), key=key)


def _progress_readiness(recent_training: dict, name: str, current_e1rm: float,
                        today_local: date) -> dict:
    """Whether this lift is ready to be pushed, or needs volume before it can be.

    The prompt caps a session at one progress attempt. This is what decides WHICH lift earns
    it — and, at least as often, that no lift should get one today.

    Computed here rather than left to the model, for the same reason `today_coverage` is: it
    is arithmetic across thirty days of sets, and a model asked to do that produces a
    confident number that does not match what the user can see in their own log.

    Five signals, checked in this order — earlier ones outrank later:

      `insufficient_data`  Fewer than `MIN_SETS_FOR_READINESS` sets in the window. Nothing
                           to read; do not let the model invent a verdict from three sets.
      `stalling`           `STALLED_NEAR_MAX_COUNT` or more near-max sets with no progress
                           set among or after them. Attempts are being made and are not
                           landing.
      `grinding`           Progress IS landing, but the median increment is under
                           `MIN_MEANINGFUL_INCREMENT_LBS`. The ceiling moves on paper and the
                           lift is stuck in practice.
      `due`               No progress in the window, or none within `PROGRESS_DUE_DAYS`,
                           and not stalling. The attempt simply has not been made.
      `progressing`        Landing at meaningful increments. Leave it alone.

    `stalling` and `grinding` are both arguments AGAINST spending the session's attempt here
    and FOR volume instead. They differ in what the user has experienced: stalling is visible
    failure, grinding is invisible — the numbers go up and the lift feels stuck anyway.

    Baseline sets are excluded throughout. A first-ever set classifies as `progress` almost
    by construction, and its increment is the whole e1RM, which would read as the healthiest
    lift in the account.
    """
    entries = []
    for day_key in sorted(recent_training):
        for entry in recent_training[day_key].get(name, []):
            if entry.get("baseline"):
                continue
            entries.append((day_key, entry))

    total = len(entries)
    progress_entries = [(d, e) for d, e in entries if e["effort"] == effort.PROGRESS]
    last_progress_day = progress_entries[-1][0] if progress_entries else None

    # ISO date keys sort and compare chronologically, which is why this can be a string
    # comparison. Same-day sets are excluded: if progress landed that day, near-max work
    # around it is not a failed attempt at a ceiling that had already moved.
    near_max_since = [
        d for d, e in entries
        if e["effort"] == effort.NEAR_MAX
        and (last_progress_day is None or d > last_progress_day)
    ]
    near_max_days = [d for d, e in entries if e["effort"] == effort.NEAR_MAX]

    # `e1rm_before`/`e1rm_after` are attached to progress entries only, in
    # `_build_recent_training`. The increment is the whole point of `grinding`.
    increments = [
        e["e1rm_after"] - e["e1rm_before"]
        for _, e in progress_entries
        if e.get("e1rm_after") is not None and e.get("e1rm_before") is not None
    ]
    median_increment = round(statistics.median(increments), 1) if increments else None

    days_since_progress = _days_since(last_progress_day, today_local)
    days_since_near_max = _days_since(near_max_days[-1] if near_max_days else None, today_local)

    if total < MIN_SETS_FOR_READINESS:
        signal = "insufficient_data"
    elif len(near_max_since) >= STALLED_NEAR_MAX_COUNT:
        signal = "stalling"
    elif median_increment is not None and median_increment < MIN_MEANINGFUL_INCREMENT_LBS:
        signal = "grinding"
    elif days_since_progress is None or days_since_progress >= PROGRESS_DUE_DAYS:
        signal = "due"
    else:
        signal = "progressing"

    return {
        "signal": signal,
        "sets_in_window": total,
        "progress_sets_in_window": len(progress_entries),
        "days_since_last_progress": days_since_progress,
        "near_max_since_last_progress": len(near_max_since),
        "days_since_last_near_max": days_since_near_max,
        "median_increment": median_increment,
    }


def _date_labels(window_start: date, today_local: date) -> dict[str, str]:
    """How to *say* each date in `recent_training`, keyed identically.

    Calendar dates read as machine output — "an easy set on 2026-08-14" is not how anyone
    talks about their training. The model is told to use these strings instead of the keys.

    Resolved here rather than described to the model, because this is date arithmetic
    against the user's local today, and asking a model to do it invites confident, wrong
    weekday names. There is exactly one right answer per date and the backend already knows
    the local date, so it computes it.
    """
    labels: dict[str, str] = {}
    cursor = window_start
    while cursor <= today_local:
        labels[cursor.isoformat()] = _day_label((today_local - cursor).days, cursor)
        cursor += timedelta(days=1)
    return labels


def _day_label(offset: int, day: date) -> str:
    """Natural reference for a date `offset` days before today.

    The bands are chosen so no phrase is ambiguous:

      0        "today"
      1        "yesterday"
      2-6      bare weekday — with today and yesterday, this covers all seven names once
      7-13     "last <weekday>" — offset 7 is the SAME weekday as today, so a bare name
               there would be indistinguishable from today
      14+      no date reference at all
    """
    if offset == 0:
        return "today"
    if offset == 1:
        return "yesterday"

    name = WEEKDAY_NAMES[day.weekday()]
    if offset < 7:
        return name
    if offset < 14:
        return f"last {name}"
    return "more than two weeks ago"


def _series_by_exercise(rows: list[dict]) -> dict[str, list[dict]]:
    """e1RM rows per exercise, oldest first."""
    series: dict[str, list[dict]] = {}
    for row in rows:
        series.setdefault(row.get("exerciseId"), []).append(row)
    for values in series.values():
        values.sort(key=lambda r: r.get("createdDatetime", ""))
    return series


def _current_e1rm(rows: list[dict]) -> float:
    """Latest value. The series is a running max, so the newest row IS the current max."""
    return effort.to_float(rows[-1].get("value")) if rows else 0.0


def _e1rm_before(series: list[dict], created: str) -> float:
    """Running-max e1RM standing immediately before `created`."""
    prior = 0.0
    for row in series:
        if row.get("createdDatetime", "") >= created:
            break
        prior = effort.to_float(row.get("value"))
    return prior


def _build_recent_training(lift_sets, name_by_id, e1rm_by_set, e1rm_series,
                           window_start: date, today_local: date) -> dict:
    """Thirty dated days, every lift key present, empty where nothing was logged.

    The empty days are the point — they are the training rhythm, and omitting them would
    make the model infer absence from missing keys rather than read it.
    """
    days: dict[str, dict[str, list]] = {}
    cursor = window_start
    while cursor <= today_local:
        days[cursor.isoformat()] = {name: [] for name in CORE_EXERCISES}
        cursor += timedelta(days=1)

    for s in lift_sets:
        name = name_by_id.get(s.get("exerciseId"))
        if not name:
            continue                                  # not one of the five

        local = effort.local_datetime(s)
        if local is None:
            continue
        key = local.date().isoformat()
        if key not in days:
            continue                                  # padding overshoot

        created = s.get("createdDatetime", "")
        after = e1rm_by_set.get(s.get("liftSetId"), 0.0)
        before = _e1rm_before(e1rm_series.get(s.get("exerciseId"), []), created)

        key_effort = effort.classify(
            effort.to_float(s.get("weight")), int(effort.to_float(s.get("reps"))), before, after
        )
        if key_effort is None:
            continue                                  # app declines to classify these too

        entry = {"at": local.isoformat(), "effort": key_effort}
        # A baseline set is a calibration measurement taken during the strength-tier
        # journey, not training. It is carried so `today_coverage` can discount it — a
        # progress set that is really a first-ever baseline says nothing about whether the
        # lift has been trained today.
        if s.get("isBaselineSet"):
            entry["baseline"] = True
        if key_effort == effort.PROGRESS:
            entry["e1rm_before"] = round(before, 1)
            entry["e1rm_after"] = round(after, 1)
        days[key][name].append(entry)

    for day in days.values():
        for sets in day.values():
            sets.sort(key=lambda e: e["at"])

    return days
