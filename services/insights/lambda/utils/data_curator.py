"""
Data curation for weekly insights generation.

Queries DynamoDB tables concurrently, then pre-computes all numeric summaries
(including strength tiers, milestones, and balance) in Python so GPT only
writes narratives.
"""

import os
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

dynamodb = boto3.resource('dynamodb')


# ---------------------------------------------------------------------------
# Strength Tier Definitions (mirrors StrengthTierDefinitions.swift)
# ---------------------------------------------------------------------------

TIER_ORDER = ['Novice', 'Beginner', 'Intermediate', 'Advanced', 'Elite', 'Legend']

CORE_EXERCISES = ['Deadlifts', 'Squats', 'Bench Press', 'Barbell Rows', 'Overhead Press']

# BW multiplier thresholds per exercise per sex.
# Each list is ordered by tier (Novice → Legend). The value is the *minimum*
# multiplier to enter that tier.
TIER_THRESHOLDS: dict[str, dict[str, list[float]]] = {
    'Deadlifts': {
        'male':   [0, 1.0, 1.5, 2.25, 3.0, 3.5],
        'female': [0, 0.5, 1.0, 1.75, 2.25, 3.0],
    },
    'Squats': {
        'male':   [0, 0.75, 1.25, 1.75, 2.5, 3.0],
        'female': [0, 0.5, 1.0, 1.5, 1.75, 2.25],
    },
    'Bench Press': {
        'male':   [0, 0.5, 1.0, 1.5, 2.0, 2.25],
        'female': [0, 0.25, 0.5, 0.75, 1.0, 1.25],
    },
    'Barbell Rows': {
        'male':   [0, 0.50, 0.75, 1.0, 1.5, 1.75],
        'female': [0, 0.25, 0.40, 0.65, 0.90, 1.20],
    },
    'Overhead Press': {
        'male':   [0, 0.40, 0.55, 0.80, 1.05, 1.35],
        'female': [0, 0.20, 0.35, 0.55, 0.75, 1.00],
    },
}

RATIO_COEFFICIENTS = {
    'Deadlifts': 1.40,
    'Squats': 1.25,
    'Bench Press': 1.00,
    'Barbell Rows': 0.825,
    'Overhead Press': 0.625,
}

BALANCE_CATEGORIES = [
    (0, 'Symmetrical'),
    (1, 'Balanced'),
    (2, 'Uneven'),
    (3, 'Skewed'),
]
# 4+ = Lopsided (default fallback)


def _get_tier_index(exercise_name: str, e1rm: float, bodyweight: float, sex: str) -> int:
    """Return the tier index (0=Novice … 5=Legend) for a given e1RM."""
    thresholds = TIER_THRESHOLDS.get(exercise_name, {}).get(sex)
    if not thresholds or bodyweight <= 0:
        return 0
    multiplier = e1rm / bodyweight
    tier_idx = 0
    for i, threshold_min in enumerate(thresholds):
        if multiplier >= threshold_min:
            tier_idx = i
    return tier_idx


def _get_tier_name(index: int) -> str:
    return TIER_ORDER[min(index, len(TIER_ORDER) - 1)]


def _balance_category(tier_indices: list[int]) -> str:
    """Determine balance category from tier spread."""
    if not tier_indices:
        return 'Unknown'
    spread = max(tier_indices) - min(tier_indices)
    for max_spread, label in BALANCE_CATEGORIES:
        if spread <= max_spread:
            return label
    return 'Lopsided'


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def curate_training_data(
    user_id: str,
    week_start: str,
    week_end: str,
) -> str:
    """
    Orchestrate all data queries and pre-processing, returning a formatted
    string ready to be used as the GPT user prompt.

    Args:
        user_id: The user's unique identifier
        week_start: Monday date "YYYY-MM-DD" of the focus week
        week_end: Sunday date "YYYY-MM-DD" of the focus week

    Returns:
        Formatted string containing all curated data for GPT
    """
    # 12-week window: from 11 weeks before focus week start to focus week end
    focus_start = date.fromisoformat(week_start)
    focus_end = date.fromisoformat(week_end)
    window_start = focus_start - timedelta(weeks=11)

    # ISO datetime boundaries for GSI queries
    # Pad by ±1 day to cover sets near midnight in any timezone (up to UTC±12)
    window_start_iso = f"{(window_start - timedelta(days=1)).isoformat()}T00:00:00.000Z"
    window_end_iso = f"{(focus_end + timedelta(days=1)).isoformat()}T23:59:59.999Z"

    # Run all queries concurrently
    with ThreadPoolExecutor(max_workers=7) as executor:
        lift_sets_future = executor.submit(_query_lift_sets, user_id, window_start_iso, window_end_iso)
        exercises_future = executor.submit(_query_exercises, user_id)
        e1rm_future = executor.submit(_query_estimated_1rm, user_id, window_start_iso, window_end_iso)
        user_props_future = executor.submit(_query_user_properties, user_id)
        plans_future = executor.submit(_query_set_plans, user_id)
        groups_future = executor.submit(_query_groups, user_id)

    lift_sets = lift_sets_future.result()
    exercises = exercises_future.result()
    e1rm_records = e1rm_future.result()
    user_properties = user_props_future.result()
    plans = plans_future.result()
    groups = groups_future.result()

    # Resolve timezone from user properties (falls back to UTC)
    user_tz_str = user_properties.get('timezone') if user_properties else None
    tz = ZoneInfo(user_tz_str) if user_tz_str else ZoneInfo('UTC')

    # Build exercise lookup
    exercise_map = {ex['exerciseItemId']: ex for ex in exercises}

    # Filter out deleted sets (baseline sets are included in volume counts)
    active_sets = [
        s for s in lift_sets
        if not s.get('deleted')
    ]

    # Group sets by week
    focus_week_sets, prior_weeks_sets = _split_by_week(active_sets, focus_start, focus_end, tz)

    # Build e1RM baselines: pre-focus max (for accurate effort classification)
    # and all-time max including focus week (for strength status)
    pre_focus_e1rm = _build_e1rm_before(e1rm_records, focus_start, tz)
    all_time_e1rm = _build_all_time_e1rm(e1rm_records)

    # Pre-compute focus week details (uses pre-focus baseline + running max)
    focus_summary = _build_focus_week_summary(focus_week_sets, exercise_map, pre_focus_e1rm, tz)

    # Pre-compute prior weeks summaries
    prior_summaries = _build_prior_weeks_summaries(prior_weeks_sets, exercise_map, focus_start, tz)

    # Format user context
    user_context = _format_user_context(user_properties, plans, focus_start, prior_weeks_sets, groups)

    # Format strength status
    strength_status = _format_strength_status(user_properties, exercise_map, all_time_e1rm)

    # Assemble the prompt
    generation_date = date.today().isoformat()
    parts = [
        f"## Focus Week: {week_start} to {week_end}",
        f"Report generated: {generation_date}",
        "",
        user_context,
        "",
        strength_status,
        "",
        "## Focus Week Detail",
        focus_summary,
        "",
        "## Prior 11 Weeks Summary",
        prior_summaries,
    ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# DynamoDB Query Helpers
# ---------------------------------------------------------------------------

def _query_lift_sets(user_id: str, start_iso: str, end_iso: str) -> list[dict]:
    """Query lift sets using the createdDatetime GSI with date range."""
    table_name = os.environ.get('LIFT_SETS_TABLE_NAME')
    table = dynamodb.Table(table_name)

    items = []
    kwargs = {
        'IndexName': 'userId-createdDatetime-index',
        'KeyConditionExpression': (
            Key('userId').eq(user_id) &
            Key('createdDatetime').between(start_iso, end_iso)
        ),
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    logger.info(f"Queried {len(items)} lift sets for user {user_id}")
    return items


def _query_exercises(user_id: str) -> list[dict]:
    """Query all non-deleted exercises for a user."""
    table_name = os.environ.get('EXERCISES_TABLE_NAME')
    table = dynamodb.Table(table_name)

    items = []
    kwargs = {'KeyConditionExpression': Key('userId').eq(user_id)}
    while True:
        response = table.query(**kwargs)
        for item in response.get('Items', []):
            if not item.get('deleted'):
                items.append(item)
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    return items


def _query_estimated_1rm(user_id: str, start_iso: str, end_iso: str) -> list[dict]:
    """Query estimated 1RM records using the createdDatetime GSI."""
    table_name = os.environ.get('ESTIMATED_1RM_TABLE_NAME')
    table = dynamodb.Table(table_name)

    items = []
    kwargs = {
        'IndexName': 'userId-createdDatetime-index',
        'KeyConditionExpression': (
            Key('userId').eq(user_id) &
            Key('createdDatetime').between(start_iso, end_iso)
        ),
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    return items


def _query_user_properties(user_id: str) -> dict | None:
    """Get user properties."""
    table_name = os.environ.get('USER_PROPERTIES_TABLE_NAME')
    table = dynamodb.Table(table_name)
    response = table.get_item(Key={'userId': user_id})
    return response.get('Item')


def _query_set_plans(user_id: str) -> list[dict]:
    """Query all non-deleted set plans for a user."""
    table_name = os.environ.get('SET_PLANS_TABLE_NAME')
    table = dynamodb.Table(table_name)

    items = []
    kwargs = {'KeyConditionExpression': Key('userId').eq(user_id)}
    while True:
        response = table.query(**kwargs)
        for item in response.get('Items', []):
            if not item.get('deleted'):
                items.append(item)
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    return items


def _query_groups(user_id: str) -> list[dict]:
    """Query all non-deleted groups for a user."""
    table_name = os.environ.get('GROUPS_TABLE_NAME')
    table = dynamodb.Table(table_name)

    items = []
    kwargs = {'KeyConditionExpression': Key('userId').eq(user_id)}
    while True:
        response = table.query(**kwargs)
        for item in response.get('Items', []):
            if not item.get('deleted'):
                items.append(item)
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    return items


# ---------------------------------------------------------------------------
# Pre-processing Helpers
# ---------------------------------------------------------------------------

def _to_float(val) -> float:
    """Convert Decimal or other numeric types to float."""
    if isinstance(val, Decimal):
        return float(val)
    return float(val)


def _calc_e1rm(weight: float, reps: int) -> float:
    """Epley formula: e1RM = weight × (1 + reps / 30)."""
    if reps == 0:
        return weight
    return weight * (1 + reps / 30)


def _effort_tier(e1rm: float, max_e1rm: float) -> str:
    """Classify a set's effort tier based on its e1RM relative to a baseline max."""
    if max_e1rm <= 0:
        return "unknown"
    ratio = e1rm / max_e1rm
    if ratio > 1.0:
        return "PR"
    elif ratio >= 0.82:
        return "Hard"
    elif ratio >= 0.70:
        return "Moderate"
    else:
        return "Easy"


def _get_local_date(created_datetime: str, tz: ZoneInfo) -> date:
    """Convert an ISO datetime string to a local date in the given timezone."""
    dt = datetime.fromisoformat(created_datetime.replace('Z', '+00:00'))
    return dt.astimezone(tz).date()


def _split_by_week(
    sets: list[dict],
    focus_start: date,
    focus_end: date,
    tz: ZoneInfo,
) -> tuple[list[dict], list[dict]]:
    """Split sets into focus week and prior weeks."""
    focus = []
    prior = []
    for s in sets:
        local_date = _get_local_date(s['createdDatetime'], tz)
        if focus_start <= local_date <= focus_end:
            focus.append(s)
        elif local_date < focus_start:
            prior.append(s)
    return focus, prior


def _build_all_time_e1rm(e1rm_records: list[dict]) -> dict[str, float]:
    """Build a map of exerciseId → all-time max e1RM value."""
    result = {}
    for rec in e1rm_records:
        if rec.get('deleted'):
            continue
        ex_id = rec.get('exerciseId')
        val = _to_float(rec.get('value', 0))
        if ex_id and val > result.get(ex_id, 0):
            result[ex_id] = val
    return result


def _build_e1rm_before(
    e1rm_records: list[dict],
    focus_start: date,
    tz: ZoneInfo,
) -> dict[str, float]:
    """Build a map of exerciseId → max e1RM from records *before* focus_start.

    This gives the correct baseline for effort tier classification during the
    focus week — sets are compared against what was known before the week began,
    not the current all-time max which may include focus-week PRs.
    """
    result = {}
    for rec in e1rm_records:
        if rec.get('deleted'):
            continue
        created = rec.get('createdDatetime', '')
        if not created:
            continue
        local_date = _get_local_date(created, tz)
        if local_date >= focus_start:
            continue
        ex_id = rec.get('exerciseId')
        val = _to_float(rec.get('value', 0))
        if ex_id and val > result.get(ex_id, 0):
            result[ex_id] = val
    return result


def _build_focus_week_summary(
    sets: list[dict],
    exercise_map: dict,
    pre_focus_e1rm: dict[str, float],
    tz: ZoneInfo,
) -> str:
    """Build detailed focus week summary text.

    Uses pre-focus e1RM as baseline, with a running max that updates as
    focus-week PRs are encountered (chronological processing). This means:
    - A set that was a PR *at the time* shows as PR
    - Subsequent sets are compared against the updated running max
    """
    if not sets:
        return "No training data logged this week."

    # Sort sets chronologically for accurate running-max PR detection
    sorted_sets = sorted(sets, key=lambda s: s['createdDatetime'])

    # Running max starts from pre-focus baseline
    running_max: dict[str, float] = dict(pre_focus_e1rm)

    # Group sets by day
    days: dict[str, list[dict]] = {}
    for s in sorted_sets:
        local_date = _get_local_date(s['createdDatetime'], tz)
        day_key = local_date.isoformat()
        if day_key not in days:
            days[day_key] = []
        days[day_key].append(s)

    # Compute per-exercise stats with running max
    exercise_stats: dict[str, dict] = {}
    for s in sorted_sets:
        ex_id = s['exerciseId']
        ex = exercise_map.get(ex_id, {})
        weight = _to_float(s.get('weight', 0))
        reps = int(s.get('reps', 0))
        e1rm = _calc_e1rm(weight, reps)

        # Classify against running max (what was known at this point in time)
        baseline = running_max.get(ex_id, 0)
        if baseline <= 0:
            # First ever set for this exercise — treat as baseline
            tier = "PR"
        else:
            tier = _effort_tier(e1rm, baseline)

        # If this is a PR, update the running max for subsequent sets
        if tier == "PR" and e1rm > running_max.get(ex_id, 0):
            running_max[ex_id] = e1rm

        if ex_id not in exercise_stats:
            exercise_stats[ex_id] = {
                'name': ex.get('name', 'Unknown'),
                'movementType': ex.get('movementType', 'Other'),
                'loadType': ex.get('loadType', 'Barbell'),
                'sets': 0,
                'max_e1rm': 0,
                'max_weight': 0,
                'max_reps': 0,
                'effort_tiers': [],
                'is_pr': False,
            }

        stats = exercise_stats[ex_id]
        stats['sets'] += 1
        stats['effort_tiers'].append(tier)
        if e1rm > stats['max_e1rm']:
            stats['max_e1rm'] = round(e1rm, 1)
        if weight > stats['max_weight']:
            stats['max_weight'] = round(weight, 1)
        if reps > stats['max_reps']:
            stats['max_reps'] = reps
        if tier == "PR":
            stats['is_pr'] = True

    # Effort distribution across all sets
    all_tiers = []
    for stats in exercise_stats.values():
        all_tiers.extend(stats['effort_tiers'])

    tier_counts: dict[str, int] = {}
    for t in all_tiers:
        tier_counts[t] = tier_counts.get(t, 0) + 1
    total_sets = len(all_tiers)

    # Movement type totals
    movement_totals: dict[str, int] = {}
    for stats in exercise_stats.values():
        mt = stats['movementType']
        movement_totals[mt] = movement_totals.get(mt, 0) + stats['sets']

    # Format output
    lines = []
    lines.append(f"Sessions: {len(days)} days ({', '.join(sorted(days.keys()))})")
    lines.append(f"Total sets: {total_sets}")
    lines.append(f"Sets by movement type: {', '.join(f'{k}: {v}' for k, v in sorted(movement_totals.items()))}")

    # Effort distribution
    tier_pcts = {t: f"{round(c / total_sets * 100)}%" for t, c in tier_counts.items()}
    lines.append(f"Effort distribution: {', '.join(f'{k}: {v}' for k, v in sorted(tier_pcts.items()))}")

    # Per-exercise detail
    lines.append("")
    lines.append("### Per-Exercise Breakdown")
    for ex_id, stats in sorted(exercise_stats.items(), key=lambda x: x[1]['name']):
        pr_flag = " **NEW PR**" if stats['is_pr'] else ""
        tier_dist = ', '.join(stats['effort_tiers'])
        lines.append(
            f"- {stats['name']} ({stats['movementType']}, {stats['loadType']}): "
            f"{stats['sets']} sets, max weight {stats['max_weight']} lbs, "
            f"max e1RM {stats['max_e1rm']} lbs, effort: [{tier_dist}]{pr_flag}"
        )

    return "\n".join(lines)


def _build_prior_weeks_summaries(
    sets: list[dict],
    exercise_map: dict,
    focus_start: date,
    tz: ZoneInfo,
) -> str:
    """Build low-token-cost summaries for the 11 prior weeks."""
    if not sets:
        return "No prior training data available."

    # Group sets by week (Monday start)
    weeks: dict[str, list[dict]] = {}
    for s in sets:
        local_date = _get_local_date(s['createdDatetime'], tz)
        monday = local_date - timedelta(days=local_date.weekday())
        week_key = monday.isoformat()
        if week_key not in weeks:
            weeks[week_key] = []
        weeks[week_key].append(s)

    lines = []
    for week_key in sorted(weeks.keys()):
        week_sets = weeks[week_key]
        # Count sessions (unique days)
        session_days = set()
        movement_counts: dict[str, int] = {}
        exercise_max_e1rm: dict[str, float] = {}

        for s in week_sets:
            local_date = _get_local_date(s['createdDatetime'], tz)
            session_days.add(local_date.isoformat())

            ex = exercise_map.get(s['exerciseId'], {})
            mt = ex.get('movementType', 'Other')
            movement_counts[mt] = movement_counts.get(mt, 0) + 1

            ex_name = ex.get('name', 'Unknown')
            weight = _to_float(s.get('weight', 0))
            reps = int(s.get('reps', 0))
            e1rm = round(_calc_e1rm(weight, reps), 1)
            if ex_name not in exercise_max_e1rm or e1rm > exercise_max_e1rm[ex_name]:
                exercise_max_e1rm[ex_name] = e1rm

        movement_str = ', '.join(f'{k}: {v}' for k, v in sorted(movement_counts.items()))
        e1rm_str = ', '.join(f'{k}: {v}' for k, v in sorted(exercise_max_e1rm.items()))

        lines.append(
            f"Week {week_key}: {len(week_sets)} sets, {len(session_days)} sessions, "
            f"by type [{movement_str}], max e1RM [{e1rm_str}]"
        )

    return "\n".join(lines)


def _format_strength_status(
    user_properties: dict | None,
    exercise_map: dict,
    all_time_e1rm: dict[str, float],
) -> str:
    """Format the Strength Status section with tiers, milestones, and balance.

    Uses all-time e1RM (including focus week) since this represents the user's
    current best, which is the correct basis for tier/milestone display.
    """
    if not user_properties:
        return "## Strength Status\nInsufficient data — no user properties available."

    bodyweight = _to_float(user_properties.get('bodyweight', 0)) if user_properties.get('bodyweight') else 0
    sex = user_properties.get('biologicalSex', 'male')  # default male if not set

    if bodyweight <= 0:
        return "## Strength Status\nBodyweight not set — cannot compute strength tiers."

    # Build exercise name → exerciseId lookup for core exercises
    name_to_id: dict[str, str] = {}
    for ex_id, ex in exercise_map.items():
        name = ex.get('name', '')
        if name in CORE_EXERCISES:
            name_to_id[name] = ex_id

    lines = ["## Strength Status", f"- Bodyweight: {bodyweight} lbs, Sex: {sex}"]

    tier_indices: list[int] = []
    exercise_tiers: dict[str, dict] = {}

    for ex_name in CORE_EXERCISES:
        ex_id = name_to_id.get(ex_name)
        current_e1rm = all_time_e1rm.get(ex_id, 0) if ex_id else 0

        tier_idx = _get_tier_index(ex_name, current_e1rm, bodyweight, sex)
        tier_name = _get_tier_name(tier_idx)
        tier_indices.append(tier_idx)

        # Compute next tier target
        thresholds = TIER_THRESHOLDS.get(ex_name, {}).get(sex, [])
        next_target_e1rm = None
        lbs_remaining = None
        if tier_idx < len(TIER_ORDER) - 1 and tier_idx + 1 < len(thresholds):
            next_target_e1rm = round(thresholds[tier_idx + 1] * bodyweight, 1)
            lbs_remaining = round(next_target_e1rm - current_e1rm, 1) if current_e1rm > 0 else None

        # Novice milestone: 50% of Beginner threshold
        novice_milestone_e1rm = None
        if tier_idx == 0 and len(thresholds) > 1:
            novice_milestone_e1rm = round(thresholds[1] * bodyweight * 0.5, 1)

        exercise_tiers[ex_name] = {
            'tier': tier_name,
            'tier_idx': tier_idx,
            'e1rm': round(current_e1rm, 1),
            'next_target': next_target_e1rm,
            'lbs_remaining': lbs_remaining,
            'novice_milestone': novice_milestone_e1rm,
        }

    # Overall tier = lowest
    overall_idx = min(tier_indices) if tier_indices else 0
    overall_tier = _get_tier_name(overall_idx)

    # Balance category
    balance = _balance_category(tier_indices)

    # Weakest / strongest
    weakest = min(exercise_tiers.items(), key=lambda x: x[1]['tier_idx']) if exercise_tiers else None
    strongest = max(exercise_tiers.items(), key=lambda x: x[1]['tier_idx']) if exercise_tiers else None

    lines.append(f"- Overall tier: **{overall_tier}** (determined by weakest exercise)")
    lines.append(f"- Balance: **{balance}**")
    if weakest:
        lines.append(f"- Weakest: {weakest[0]} ({weakest[1]['tier']})")
    if strongest:
        lines.append(f"- Strongest: {strongest[0]} ({strongest[1]['tier']})")

    lines.append("")
    lines.append("### Per-Exercise Tier Status")
    for ex_name in CORE_EXERCISES:
        info = exercise_tiers.get(ex_name)
        if not info:
            lines.append(f"- {ex_name}: No data")
            continue

        parts = [f"{ex_name}: **{info['tier']}** (e1RM: {info['e1rm']} lbs)"]

        if info['tier_idx'] == 0 and info['novice_milestone']:
            # Show novice milestone progress
            if info['e1rm'] >= info['novice_milestone']:
                parts.append(f"— Novice milestone achieved ({info['novice_milestone']} lbs)")
            else:
                remaining = round(info['novice_milestone'] - info['e1rm'], 1)
                parts.append(f"— {remaining} lbs to Novice milestone ({info['novice_milestone']} lbs)")

        if info['next_target'] and info['lbs_remaining'] is not None:
            next_tier = _get_tier_name(info['tier_idx'] + 1)
            if info['lbs_remaining'] > 0:
                parts.append(f"— {info['lbs_remaining']} lbs to {next_tier} ({info['next_target']} lbs)")
            else:
                parts.append(f"— {next_tier} threshold reached!")

        lines.append(f"- {' '.join(parts)}")

    return "\n".join(lines)


def _format_user_context(
    user_properties: dict | None,
    plans: list[dict],
    focus_start: date,
    prior_sets: list[dict],
    groups: list[dict] | None = None,
) -> str:
    """Format user context information."""
    lines = ["## User Context"]

    # User properties
    if user_properties:
        bw = user_properties.get('bodyweight')
        if bw:
            lines.append(f"- Bodyweight: {_to_float(bw)} lbs")
        min_reps = user_properties.get('minReps')
        max_reps = user_properties.get('maxReps')
        if min_reps or max_reps:
            lines.append(f"- Rep range preference: {min_reps}-{max_reps}")

    # Active set plans
    if plans:
        plan_names = [p.get('name', 'Unnamed') for p in plans]
        lines.append(f"- Set plans: {', '.join(plan_names)}")

    # Exercise groups
    if groups:
        group_names = [g.get('name', 'Unnamed') for g in groups]
        lines.append(f"- Exercise groups: {', '.join(group_names)}")
    # First week flag
    is_first_week = len(prior_sets) == 0
    if is_first_week:
        lines.append("- **This is the user's first week of training data.** Use 'establishing baselines' framing.")

    return "\n".join(lines)


def curate_starter_data(user_id: str) -> str | None:
    """
    Curate lightweight data for starter insight generation.

    Only queries user properties, exercises, and all-time e1RM records.
    Returns None if fewer than 5 core exercises have e1RM data.

    Args:
        user_id: The user's unique identifier

    Returns:
        Formatted string for GPT, or None if not enough data
    """
    # Query all-time e1RM records (no date window)
    with ThreadPoolExecutor(max_workers=3) as executor:
        user_props_future = executor.submit(_query_user_properties, user_id)
        exercises_future = executor.submit(_query_exercises, user_id)
        e1rm_future = executor.submit(_query_all_estimated_1rm, user_id)

    user_properties = user_props_future.result()
    exercises = exercises_future.result()
    e1rm_records = e1rm_future.result()

    exercise_map = {ex['exerciseItemId']: ex for ex in exercises}
    all_time_e1rm = _build_all_time_e1rm(e1rm_records)

    # Check that all 5 core exercises have e1RM data
    name_to_id: dict[str, str] = {}
    for ex_id, ex in exercise_map.items():
        name = ex.get('name', '')
        if name in CORE_EXERCISES:
            name_to_id[name] = ex_id

    core_with_data = sum(1 for name in CORE_EXERCISES if all_time_e1rm.get(name_to_id.get(name), 0) > 0)
    if core_with_data < 5:
        return None

    strength_status = _format_strength_status(user_properties, exercise_map, all_time_e1rm)

    # Compute overall tier name to pass explicitly
    bodyweight = _to_float(user_properties.get('bodyweight', 0)) if user_properties and user_properties.get('bodyweight') else 0
    sex = user_properties.get('biologicalSex', 'male') if user_properties else 'male'
    tier_indices = []
    for ex_name in CORE_EXERCISES:
        ex_id = name_to_id.get(ex_name)
        current_e1rm = all_time_e1rm.get(ex_id, 0) if ex_id else 0
        tier_indices.append(_get_tier_index(ex_name, current_e1rm, bodyweight, sex))
    overall_tier = _get_tier_name(min(tier_indices)) if tier_indices else 'Novice'

    generation_date = date.today().isoformat()
    parts = [
        "## Starter Insight Data",
        f"Report generated: {generation_date}",
        "",
        f"IMPORTANT: The user's overall strength tier is **{overall_tier}**. "
        f"You MUST refer to this tier by name when congratulating them. "
        f"Do NOT use any per-exercise tier name as the overall tier.",
        "",
        "## User Context",
    ]

    if user_properties:
        bw = user_properties.get('bodyweight')
        if bw:
            parts.append(f"- Bodyweight: {_to_float(bw)} lbs, Sex: {sex}")
        else:
            parts.append(f"- Sex: {sex}")

    parts.extend(["", strength_status])

    return "\n".join(parts)


def curate_tier_unlock_data(user_id: str, tier_name: str) -> str | None:
    """
    Curate data for tier unlock insight generation.

    Server-side validates that the user's computed overall tier matches the
    requested tier. Returns None if tier mismatch or cache already exists.

    Args:
        user_id: The user's unique identifier
        tier_name: Tier name to generate for (e.g. 'Beginner', 'Intermediate')

    Returns:
        Formatted string for GPT, or None if validation fails
    """
    from utils.cache import get_cached_tier_unlock

    # Check cache first (idempotency)
    if get_cached_tier_unlock(user_id, tier_name):
        return None

    # Query data
    with ThreadPoolExecutor(max_workers=3) as executor:
        user_props_future = executor.submit(_query_user_properties, user_id)
        exercises_future = executor.submit(_query_exercises, user_id)
        e1rm_future = executor.submit(_query_all_estimated_1rm, user_id)

    user_properties = user_props_future.result()
    exercises = exercises_future.result()
    e1rm_records = e1rm_future.result()

    exercise_map = {ex['exerciseItemId']: ex for ex in exercises}
    all_time_e1rm = _build_all_time_e1rm(e1rm_records)

    bodyweight = _to_float(user_properties.get('bodyweight', 0)) if user_properties and user_properties.get('bodyweight') else 0
    sex = user_properties.get('biologicalSex', 'male') if user_properties else 'male'

    if bodyweight <= 0:
        return None

    # Build per-exercise tier data
    name_to_id: dict[str, str] = {}
    for ex_id, ex in exercise_map.items():
        name = ex.get('name', '')
        if name in CORE_EXERCISES:
            name_to_id[name] = ex_id

    tier_indices = []
    exercise_tiers: dict[str, dict] = {}
    for ex_name in CORE_EXERCISES:
        ex_id = name_to_id.get(ex_name)
        current_e1rm = all_time_e1rm.get(ex_id, 0) if ex_id else 0
        tier_idx = _get_tier_index(ex_name, current_e1rm, bodyweight, sex)
        tier_indices.append(tier_idx)
        exercise_tiers[ex_name] = {
            'tier': _get_tier_name(tier_idx),
            'tier_idx': tier_idx,
        }

    # Compute overall tier and validate
    computed_overall_idx = min(tier_indices) if tier_indices else 0
    computed_overall = _get_tier_name(computed_overall_idx)

    if computed_overall.lower() != tier_name.lower():
        logger.info(f"Tier mismatch for user {user_id}: requested {tier_name}, computed {computed_overall}")
        return None

    # Determine previous tier by checking actual unlock history.
    # If no prior tier unlock exists, this is the user's first tier regardless
    # of tier index (e.g. first-ever unlock at Beginner should NOT say "from Novice").
    from utils.cache import get_all_tier_unlocks

    existing_unlocks = get_all_tier_unlocks(user_id)
    # Exclude the current tier being generated (may already be cached in a race)
    prior_unlocks = [
        u for u in existing_unlocks
        if u.get('insightWeek', '') != f'tier-{tier_name.lower()}'
    ]

    if prior_unlocks and computed_overall_idx > 0:
        prev_tier = _get_tier_name(computed_overall_idx - 1)
    else:
        prev_tier = None

    # Determine weakest (bottleneck) and strongest
    weakest = min(exercise_tiers.items(), key=lambda x: x[1]['tier_idx'])
    strongest = max(exercise_tiers.items(), key=lambda x: x[1]['tier_idx'])

    # Balance
    balance = _balance_category(tier_indices)

    # Distance to next tier (relative terms only)
    next_tier = _get_tier_name(computed_overall_idx + 1) if computed_overall_idx < len(TIER_ORDER) - 1 else None

    # Per-exercise relative performance (tier name only, no numbers)
    exercise_lines = []
    for ex_name in CORE_EXERCISES:
        info = exercise_tiers[ex_name]
        exercise_lines.append(f"- {ex_name}: {info['tier']}")

    # Build prompt data (deliberately excludes specific weights/dates/set counts)
    generation_date = date.today().isoformat()
    parts = [
        "## Tier Unlock Data",
        f"Report generated: {generation_date}",
        "",
        f"IMPORTANT: The user's overall strength tier is **{computed_overall}**.",
        f"You MUST refer to this tier by name when congratulating them.",
        f"Do NOT use any per-exercise tier name as the overall tier.",
    ]

    if prev_tier:
        parts.append(f"Previous overall tier: **{prev_tier}** (the user has advanced from {prev_tier} to {computed_overall}).")
    else:
        parts.append("This is the user's FIRST overall tier (no previous tier).")

    is_first_tier = (prev_tier is None)
    parts.append(f"Is first tier unlock: {'yes' if is_first_tier else 'no'}")

    parts.extend([
        "",
        "## Per-Exercise Tier Status (relative only)",
        *exercise_lines,
        "",
        f"- Balance category: {balance}",
        f"- Bottleneck exercise (weakest): {weakest[0]} ({weakest[1]['tier']})",
        f"- Strongest exercise: {strongest[0]} ({strongest[1]['tier']})",
    ])

    if next_tier:
        parts.append(f"- Next overall tier: {next_tier}")
    else:
        parts.append("- User has reached the highest tier!")

    parts.extend([
        "",
        "## User Context",
        f"- Sex: {sex}",
    ])

    return "\n".join(parts)


def _query_all_estimated_1rm(user_id: str) -> list[dict]:
    """Query all estimated 1RM records for a user (no date range)."""
    table_name = os.environ.get('ESTIMATED_1RM_TABLE_NAME')
    table = dynamodb.Table(table_name)

    items = []
    kwargs = {
        'IndexName': 'userId-createdDatetime-index',
        'KeyConditionExpression': Key('userId').eq(user_id),
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    return items


def has_sets_in_week(user_id: str, week_start: str, week_end: str) -> bool:
    """Check if a user has any lift sets in a given week (quick existence check)."""
    table_name = os.environ.get('LIFT_SETS_TABLE_NAME')
    table = dynamodb.Table(table_name)

    start_iso = f"{week_start}T00:00:00.000Z"
    end_iso = f"{week_end}T23:59:59.999Z"

    response = table.query(
        IndexName='userId-createdDatetime-index',
        KeyConditionExpression=(
            Key('userId').eq(user_id) &
            Key('createdDatetime').between(start_iso, end_iso)
        ),
        Limit=1,
        Select='COUNT',
    )

    return response.get('Count', 0) > 0
