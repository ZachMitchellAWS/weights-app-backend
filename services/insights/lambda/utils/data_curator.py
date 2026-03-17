"""
Data curation for weekly insights generation.

Queries 6 DynamoDB tables concurrently, then pre-computes all numeric summaries
in Python so GPT only writes narratives.
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
    focus_start_iso = f"{(focus_start - timedelta(days=1)).isoformat()}T00:00:00.000Z"

    # Run all 7 queries concurrently
    with ThreadPoolExecutor(max_workers=7) as executor:
        lift_sets_future = executor.submit(_query_lift_sets, user_id, window_start_iso, window_end_iso)
        exercises_future = executor.submit(_query_exercises, user_id)
        e1rm_future = executor.submit(_query_estimated_1rm, user_id, window_start_iso, window_end_iso)
        accessory_future = executor.submit(_query_accessory_checkins, user_id, focus_start_iso, window_end_iso)
        user_props_future = executor.submit(_query_user_properties, user_id)
        templates_future = executor.submit(_query_set_plan_templates, user_id)

    lift_sets = lift_sets_future.result()
    exercises = exercises_future.result()
    e1rm_records = e1rm_future.result()
    accessory_checkins = accessory_future.result()
    user_properties = user_props_future.result()
    templates = templates_future.result()

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

    # Build all-time e1RM max per exercise (for PR detection)
    all_time_e1rm = _build_all_time_e1rm(e1rm_records)

    # Pre-compute focus week details
    focus_summary = _build_focus_week_summary(focus_week_sets, exercise_map, all_time_e1rm, tz)

    # Pre-compute prior weeks summaries
    prior_summaries = _build_prior_weeks_summaries(prior_weeks_sets, exercise_map, focus_start, tz)

    # Format accessory goals
    accessory_summary = _format_accessory_goals(accessory_checkins)

    # Format user context
    user_context = _format_user_context(user_properties, templates, focus_start, prior_weeks_sets)

    # Assemble the prompt
    parts = [
        f"## Focus Week: {week_start} to {week_end}",
        "",
        user_context,
        "",
        "## Focus Week Detail",
        focus_summary,
        "",
        "## Prior 11 Weeks Summary",
        prior_summaries,
    ]

    if accessory_summary:
        parts.extend(["", "## Accessory Goals (Focus Week)", accessory_summary])

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


def _query_accessory_checkins(user_id: str, start_iso: str, end_iso: str) -> list[dict]:
    """Query accessory goal checkins for the focus week."""
    table_name = os.environ.get('ACCESSORY_GOAL_CHECKINS_TABLE_NAME')
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
        for item in response.get('Items', []):
            if not item.get('deleted'):
                items.append(item)
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


def _query_set_plan_templates(user_id: str) -> list[dict]:
    """Query all non-deleted set plan templates for a user."""
    table_name = os.environ.get('SET_PLAN_TEMPLATES_TABLE_NAME')
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
    """Classify a set's effort tier based on its e1RM relative to all-time max."""
    if max_e1rm <= 0:
        return "unknown"
    ratio = e1rm / max_e1rm
    if ratio > 1.0:
        return "PR"
    elif ratio >= 0.92:
        return "Redline"
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


def _build_focus_week_summary(
    sets: list[dict],
    exercise_map: dict,
    all_time_e1rm: dict[str, float],
    tz: ZoneInfo,
) -> str:
    """Build detailed focus week summary text."""
    if not sets:
        return "No training data logged this week."

    # Group sets by day
    days = {}
    for s in sets:
        local_date = _get_local_date(s['createdDatetime'], tz)
        day_key = local_date.isoformat()
        if day_key not in days:
            days[day_key] = []
        days[day_key].append(s)

    # Compute per-exercise stats
    exercise_stats = {}  # exerciseId → {name, sets, max_e1rm, effort_tiers, movement_type}
    for s in sets:
        ex_id = s['exerciseId']
        ex = exercise_map.get(ex_id, {})
        weight = _to_float(s.get('weight', 0))
        reps = int(s.get('reps', 0))
        e1rm = _calc_e1rm(weight, reps)
        max_e1rm = all_time_e1rm.get(ex_id, e1rm)
        tier = _effort_tier(e1rm, max_e1rm)

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

    tier_counts = {}
    for t in all_tiers:
        tier_counts[t] = tier_counts.get(t, 0) + 1
    total_sets = len(all_tiers)

    # Movement type totals
    movement_totals = {}
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

    # Day-by-day detail
    lines.append("")
    lines.append("### Day-by-Day Sets")
    for day_key in sorted(days.keys()):
        day_sets = days[day_key]
        lines.append(f"\n**{day_key}** ({len(day_sets)} sets):")
        for s in day_sets:
            ex = exercise_map.get(s['exerciseId'], {})
            weight = _to_float(s.get('weight', 0))
            reps = int(s.get('reps', 0))
            e1rm = round(_calc_e1rm(weight, reps), 1)
            rir_str = f", RIR {s['rir']}" if s.get('rir') is not None else ""
            lines.append(f"  {ex.get('name', 'Unknown')}: {weight} lbs × {reps} reps (e1RM: {e1rm}){rir_str}")

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
    weeks = {}
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
        movement_counts = {}
        exercise_max_e1rm = {}

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


def _format_accessory_goals(checkins: list[dict]) -> str:
    """Format accessory goal checkins for the focus week."""
    if not checkins:
        return ""

    # Group by metric type
    by_type = {}
    for c in checkins:
        mt = c.get('metricType', 'unknown')
        if mt not in by_type:
            by_type[mt] = []
        by_type[mt].append(_to_float(c.get('value', 0)))

    lines = []
    for metric_type, values in sorted(by_type.items()):
        avg_val = round(sum(values) / len(values), 1)
        lines.append(f"- {metric_type}: {len(values)} entries, avg {avg_val}, range {min(values)}-{max(values)}")

    return "\n".join(lines)


def _format_user_context(
    user_properties: dict | None,
    templates: list[dict],
    focus_start: date,
    prior_sets: list[dict],
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

    # Active templates
    if templates:
        template_names = [t.get('name', 'Unnamed') for t in templates]
        lines.append(f"- Set plan templates: {', '.join(template_names)}")

    # First week flag
    is_first_week = len(prior_sets) == 0
    if is_first_week:
        lines.append("- **This is the user's first week of training data.** Use 'establishing baselines' framing.")

    return "\n".join(lines)


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
