"""
Insights service Lambda handler.

Three invocation pathways:
1. SCHEDULE_TASK — async from checkin Lambda after lift set creation
2. PROCESS_TASKS — EventBridge cron every 15 min, processes ripe tasks
3. GET_INSIGHTS — API Gateway GET /insights/weekly
"""

import os
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from utils.response import create_response
from utils.entitlement_check import check_premium
from utils.task_manager import (
    schedule_task,
    get_ripe_tasks,
    claim_task,
    delete_task,
    get_task,
    create_processing_task,
    get_insight_week,
    STALE_THRESHOLD_SECONDS,
)
from utils.cache import get_cached_insights, put_cached_insights
from utils.data_curator import curate_training_data, has_sets_in_week

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Module-level cache for the system prompt
_system_prompt = None


def _get_system_prompt() -> str:
    """Load the app_context.md system prompt (cached across warm invocations)."""
    global _system_prompt
    if _system_prompt is not None:
        return _system_prompt

    context_path = Path(__file__).parent.parent / "context" / "app_context.md"
    with open(context_path, 'r') as f:
        _system_prompt = f.read()
    return _system_prompt


def _get_week_end(week_start: str) -> str:
    """Get the Sunday date string for a given Monday date string."""
    monday = date.fromisoformat(week_start)
    sunday = monday + timedelta(days=6)
    return sunday.isoformat()


def _get_previous_completed_week() -> tuple[str, str]:
    """
    Get the most recent completed Mon-Sun week.

    Returns:
        Tuple of (week_start, week_end) as "YYYY-MM-DD" strings
    """
    today = datetime.now(timezone.utc).date()
    # Most recent Monday that's at least 7 days ago (i.e. the full week has ended)
    days_since_monday = today.weekday()  # 0=Mon
    # If today is Monday, the previous completed week started 7 days ago
    # If today is Tuesday, it started 8 days ago, etc.
    prev_monday = today - timedelta(days=days_since_monday + 7)
    prev_sunday = prev_monday + timedelta(days=6)
    return prev_monday.isoformat(), prev_sunday.isoformat()


def _generate_and_cache(user_id: str, week_start: str, week_end: str) -> dict:
    """
    Generate insights via OpenAI and write to cache.

    Args:
        user_id: The user's unique identifier
        week_start: Monday date "YYYY-MM-DD"
        week_end: Sunday date "YYYY-MM-DD"

    Returns:
        Dict with sections, weekStartDate, weekEndDate, generatedAt
    """
    # Lazy import to avoid loading openai on non-generation paths
    from utils.openai_client import generate_insights

    system_prompt = _get_system_prompt()
    curated_data = curate_training_data(user_id, week_start, week_end)
    model = os.environ.get('OPENAI_MODEL', 'gpt-5.4')

    sections = generate_insights(system_prompt, curated_data)

    put_cached_insights(user_id, week_start, sections, model)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {
        "weekStartDate": week_start,
        "weekEndDate": week_end,
        "generatedAt": now_utc,
        "sections": sections,
    }


# ===========================================================================
# Pathway 1: SCHEDULE_TASK (async invoke from checkin Lambda)
# ===========================================================================

def schedule_insight_task(user_id: str, timezone_str: str, created_datetime: str) -> dict:
    """
    Schedule an insight generation task for a user's week.

    Called async from checkin Lambda after lift set creation. Does a real
    entitlement check before creating the task.
    """
    # Authoritative premium check (client-side flag was just an optimization)
    if not check_premium(user_id):
        logger.info(f"User {user_id} is not premium, skipping task scheduling")
        return {"status": "skipped", "reason": "not_premium"}

    created = schedule_task(user_id, timezone_str, created_datetime)
    return {"status": "created" if created else "already_exists"}


# ===========================================================================
# Pathway 2: PROCESS_TASKS (EventBridge cron)
# ===========================================================================

def process_ripe_tasks() -> dict:
    """
    Process all ripe insight generation tasks.

    Called by EventBridge every 15 minutes. Processes tasks sequentially
    to respect OpenAI rate limits.
    """
    tasks = get_ripe_tasks(limit=10)
    logger.info(f"Found {len(tasks)} ripe tasks to process")

    processed = 0
    errors = 0

    for task in tasks:
        user_id = task['userId']
        insight_week = task['insightWeek']

        try:
            # Claim the task atomically
            if not claim_task(user_id, insight_week):
                continue

            # Verify entitlement is still active
            if not check_premium(user_id):
                logger.info(f"User {user_id} no longer premium, deleting task")
                delete_task(user_id, insight_week)
                continue

            # Generate insights
            week_end = _get_week_end(insight_week)
            _generate_and_cache(user_id, insight_week, week_end)

            # Clean up task
            delete_task(user_id, insight_week)
            processed += 1

        except Exception as e:
            logger.error(f"Error processing task for user {user_id}, week {insight_week}: {e}")
            errors += 1
            # Task stays in "processing" state — will be reclaimed after staleness timeout

    logger.info(f"Processed {processed} tasks, {errors} errors")
    return {"processed": processed, "errors": errors}


# ===========================================================================
# Pathway 3: GET_INSIGHTS (API Gateway)
# ===========================================================================

def get_weekly_insights(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get weekly training insights for a user.

    Fast path: return cached insight. Slow path: generate ad-hoc if needed.
    """
    # Check entitlement
    if not check_premium(user_id):
        return create_response(403, {"error": "Premium subscription required for weekly insights"})

    # Determine the most recent completed week
    week_start, week_end = _get_previous_completed_week()

    # Fast path: check cache
    cached = get_cached_insights(user_id, week_start)
    if cached:
        return create_response(200, {
            "weekStartDate": week_start,
            "weekEndDate": week_end,
            "generatedAt": cached.get('generatedAt'),
            "sections": cached.get('sections', []),
        })

    # Check task table
    task = get_task(user_id, week_start)

    if task:
        task_status = task.get('taskStatus')
        processing_started = task.get('processingStartedAt')

        if task_status == 'processing' and processing_started:
            # Check if stale
            now = datetime.now(timezone.utc)
            started_dt = datetime.fromisoformat(processing_started.replace('Z', '+00:00'))
            elapsed = (now - started_dt).total_seconds()

            if elapsed < STALE_THRESHOLD_SECONDS:
                # Currently being processed by cron — tell client to check back
                return create_response(202, {
                    "status": "processing",
                    "message": "Your weekly insights are being generated. Check back in a minute!",
                })
            # Stale — re-claim and generate ad-hoc
            if not claim_task(user_id, week_start):
                return create_response(202, {
                    "status": "processing",
                    "message": "Your weekly insights are being generated. Check back in a minute!",
                })

        elif task_status == 'pending':
            eligible = task.get('eligibleAfterUtc', '')
            now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

            if eligible > now_utc:
                # Week hasn't ended yet
                return create_response(200, {
                    "sections": [],
                    "message": "No insights available yet. Log some sets and check back next week!",
                })

            # Eligible — claim and generate ad-hoc
            if not claim_task(user_id, week_start):
                return create_response(202, {
                    "status": "processing",
                    "message": "Your weekly insights are being generated. Check back in a minute!",
                })
        else:
            # Unknown status — try to claim
            if not claim_task(user_id, week_start):
                return create_response(202, {
                    "status": "processing",
                    "message": "Your weekly insights are being generated. Check back in a minute!",
                })
    else:
        # No task exists — check if there's data for this week
        if not has_sets_in_week(user_id, week_start, week_end):
            return create_response(200, {
                "sections": [],
                "message": "No insights available yet. Log some sets and check back next week!",
            })

        # Data exists but no task — create in processing state and generate
        _, eligible_after_utc = get_insight_week(
            f"{week_end}T23:59:59Z",
            'UTC',
        )
        create_processing_task(user_id, week_start, eligible_after_utc)

    # If we got here, we've claimed/created the task — generate ad-hoc
    try:
        result = _generate_and_cache(user_id, week_start, week_end)
        delete_task(user_id, week_start)
        return create_response(200, result)
    except Exception as e:
        logger.error(f"Ad-hoc generation failed for user {user_id}: {e}")
        # Task stays in processing — will be reclaimed after staleness
        return create_response(503, {
            "error": "Insight generation temporarily unavailable. Please try again later.",
        })


# ===========================================================================
# Main Handler
# ===========================================================================

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for insights service.

    Routes based on invocation type:
    - EventBridge cron → PROCESS_TASKS
    - Async invoke from checkin → SCHEDULE_TASK
    - API Gateway → GET /insights/weekly
    """
    invocation_type = event.get("invocationType")

    # Pathway 1: EventBridge cron
    if invocation_type == "PROCESS_TASKS":
        logger.info("Processing ripe insight tasks (EventBridge cron)")
        return process_ripe_tasks()

    # Pathway 2: Async invoke from checkin Lambda
    if invocation_type == "SCHEDULE_TASK":
        user_id = event.get("userId")
        tz = event.get("createdTimezone")
        created_dt = event.get("createdDatetime")
        if not user_id or not tz or not created_dt:
            logger.error(f"SCHEDULE_TASK missing required fields: {event}")
            return {"error": "Missing userId, createdTimezone, or createdDatetime"}
        logger.info(f"Scheduling insight task for user {user_id}")
        return schedule_insight_task(user_id, tz, created_dt)

    # Pathway 3: API Gateway (GET /insights/weekly)
    http_method = event.get("httpMethod")
    path = event.get("path", "")

    if http_method and event.get("requestContext", {}).get("authorizer"):
        user_id = event["requestContext"]["authorizer"]["userId"]
        logger.info(f"Insights request: {http_method} {path} for user {user_id}")

        if http_method == "GET" and path.endswith("/insights/weekly"):
            return get_weekly_insights(event, user_id)

    return create_response(404, {"error": f"Route not found: {event.get('httpMethod')} {event.get('path', '')}"})
