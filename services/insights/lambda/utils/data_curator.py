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

        # Compute next tier target.
        # Tier-threshold-derived lbs values are rounded to whole numbers to
        # match the iOS app's display (frontend tier ranges, milestone targets,
        # and "X lbs to next tier" all round, not truncate). Keeps narrative
        # text consistent with what the user sees on screen.
        thresholds = TIER_THRESHOLDS.get(ex_name, {}).get(sex, [])
        next_target_e1rm = None
        lbs_remaining = None
        if tier_idx < len(TIER_ORDER) - 1 and tier_idx + 1 < len(thresholds):
            next_target_e1rm = round(thresholds[tier_idx + 1] * bodyweight)
            lbs_remaining = round(next_target_e1rm - current_e1rm) if current_e1rm > 0 else None

        # Novice milestone: 50% of Beginner threshold
        novice_milestone_e1rm = None
        if tier_idx == 0 and len(thresholds) > 1:
            novice_milestone_e1rm = round(thresholds[1] * bodyweight * 0.5)

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
                remaining = round(info['novice_milestone'] - info['e1rm'])
                parts.append(f"— {remaining} lbs to Novice milestone ({info['novice_milestone']} lbs)")

        if info['next_target'] and info['lbs_remaining'] is not None:
            next_tier = _get_tier_name(info['tier_idx'] + 1)
            if info['lbs_remaining'] > 0:
                parts.append(f"— {info['lbs_remaining']} lbs to {next_tier} ({info['next_target']} lbs)")
            else:
                parts.append(f"— {next_tier} threshold reached!")

        lines.append(f"- {' '.join(parts)}")

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

    # The strength-tier insight is a one-time celebration of the user's
    # STARTING tier (whatever tier they first qualified for) — it is NOT
    # generated for subsequent tier-ups. We detect the starting tier by
    # checking whether any prior tier-unlock items already exist for this
    # user; if any do, this request is for a subsequent tier and we skip
    # generation entirely (handler returns a no-op response).
    from utils.cache import get_all_tier_unlocks

    existing_unlocks = get_all_tier_unlocks(user_id)
    # Exclude the current tier being generated (may already be cached in a race)
    prior_unlocks = [
        u for u in existing_unlocks
        if u.get('insightWeek', '') != f'tier-{tier_name.lower()}'
    ]

    if prior_unlocks:
        logger.info(
            f"User {user_id} already has prior tier unlock(s) "
            f"{[u.get('insightWeek') for u in prior_unlocks]}; "
            f"skipping insight generation for tier '{tier_name}' "
            f"(insight is starting-tier-only)."
        )
        return None

    # First-tier path: no prior unlocks, no "from <prev_tier>" framing in the prompt.
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

