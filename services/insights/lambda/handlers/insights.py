"""
Insights service Lambda handler.

Nine invocation pathways:
1. SCHEDULE_TASK — async from checkin Lambda after lift set creation
2. PROCESS_TASKS — EventBridge cron every 15 min, processes ripe tasks
3. GET_INSIGHTS — API Gateway GET /insights/weekly
4. GENERATE_AUDIO — async self-invoke to generate TTS audio after insights are cached
5. GET_STARTER_INSIGHT — API Gateway GET /insights/starter (all users)
6. GENERATE_STARTER_AUDIO — async self-invoke to generate TTS for starter insight
7. POST_TIER_UNLOCK — API Gateway POST /insights/tier-unlock (all users)
8. GET_TIER_UNLOCKS — API Gateway GET /insights/tier-unlocks (all users)
9. GENERATE_TIER_UNLOCK_AUDIO — async self-invoke to generate TTS for tier unlock
"""

import json
import os
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import boto3

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
from utils.cache import (
    get_cached_insights, put_cached_insights, update_audio_keys,
    get_cached_starter, put_cached_starter, update_starter_audio_key,
    get_cached_tier_unlock, put_cached_tier_unlock, get_all_tier_unlocks,
    update_tier_unlock_audio_key,
)
from utils.data_curator import curate_training_data, has_sets_in_week, curate_starter_data, curate_tier_unlock_data
from utils.sentry_init import init_sentry, set_sentry_user
import sentry_sdk

init_sentry()

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Module-level cache for the system prompt
_system_prompt = None

_s3_client = None


def _get_s3_client():
    """Get S3 client with module-level cache."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client('s3')
    return _s3_client


PRESIGNED_URL_EXPIRY_SECONDS = 21600  # 6 hours


def _attach_audio_urls(sections: list[dict], audio_keys: list[str]) -> None:
    """Attach presigned S3 URLs to section dicts (mutates in place)."""
    bucket = os.environ.get('INSIGHTS_AUDIO_BUCKET')
    if not bucket:
        return
    s3 = _get_s3_client()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=PRESIGNED_URL_EXPIRY_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    for section, key in zip(sections, audio_keys):
        try:
            url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': key},
                ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
            )
            section['audioUrl'] = url
            section['audioUrlExpiresAt'] = expires_at
        except Exception as e:
            logger.warning(f"Failed to generate presigned URL for {key}: {e}")


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
    Generate insights via OpenAI and write to cache, then kick off async TTS.

    Args:
        user_id: The user's unique identifier
        week_start: Monday date "YYYY-MM-DD"
        week_end: Sunday date "YYYY-MM-DD"

    Returns:
        Dict with sections, weekStartDate, weekEndDate, generatedAt (no audio URLs)
    """
    # Lazy import to avoid loading openai on non-generation paths
    from utils.openai_client import generate_insights

    system_prompt = _get_system_prompt()
    curated_data = curate_training_data(user_id, week_start, week_end)
    model = os.environ.get('OPENAI_MODEL', 'gpt-5.4')

    sections = generate_insights(system_prompt, curated_data)

    put_cached_insights(user_id, week_start, sections, model)

    # Fire-and-forget: kick off TTS generation asynchronously
    _invoke_generate_audio(user_id, week_start)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {
        "weekStartDate": week_start,
        "weekEndDate": week_end,
        "generatedAt": now_utc,
        "sections": sections,
    }


def _invoke_generate_audio(user_id: str, week_start: str) -> None:
    """Async self-invoke to generate TTS audio for cached insights."""
    function_name = os.environ.get('INSIGHTS_FUNCTION_NAME')
    if not function_name:
        logger.warning("INSIGHTS_FUNCTION_NAME not set, skipping TTS generation")
        return

    try:
        lambda_client = boto3.client('lambda')
        payload = json.dumps({
            'invocationType': 'GENERATE_AUDIO',
            'userId': user_id,
            'weekStart': week_start,
        })
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=payload,
        )
        logger.info(f"Async-invoked TTS generation for user {user_id}, week {week_start}")
    except Exception as e:
        logger.warning(f"Failed to invoke TTS generation: {e}")


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
# Pathway 3: GENERATE_AUDIO (async self-invoke)
# ===========================================================================

def generate_audio(user_id: str, week_start: str) -> dict:
    """
    Generate TTS audio for cached insights and update the cache item.

    Called asynchronously after insights are generated and cached.
    """
    from utils.tts import generate_section_audio

    cached = get_cached_insights(user_id, week_start)
    if not cached:
        logger.warning(f"No cached insights for user {user_id}, week {week_start} — skipping TTS")
        return {"status": "skipped", "reason": "no_cache"}

    if cached.get('audioKeys'):
        logger.info(f"Audio already exists for user {user_id}, week {week_start} — skipping")
        return {"status": "skipped", "reason": "already_exists"}

    sections = cached.get('sections', [])
    if not sections:
        return {"status": "skipped", "reason": "no_sections"}

    try:
        audio_keys = generate_section_audio(sections, user_id, week_start)
        update_audio_keys(user_id, week_start, audio_keys)
        logger.info(f"TTS audio generated and cached for user {user_id}, week {week_start}")
        return {"status": "completed", "audioKeys": audio_keys}
    except Exception as e:
        logger.error(f"TTS generation failed for user {user_id}, week {week_start}: {e}")
        return {"status": "error", "error": str(e)}


# ===========================================================================
# Pathway 4: GET_INSIGHTS (API Gateway)
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
        sections = cached.get('sections', [])
        audio_keys = cached.get('audioKeys')
        if audio_keys:
            _attach_audio_urls(sections, audio_keys)
        return create_response(200, {
            "weekStartDate": week_start,
            "weekEndDate": week_end,
            "generatedAt": cached.get('generatedAt'),
            "sections": sections,
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
# Pathway 5: GET_STARTER_INSIGHT (API Gateway GET /insights/starter)
# ===========================================================================

# Module-level cache for the starter system prompt
_starter_prompt = None


def _get_starter_prompt() -> str:
    """Load the starter_context.md system prompt (cached across warm invocations)."""
    global _starter_prompt
    if _starter_prompt is not None:
        return _starter_prompt

    context_path = Path(__file__).parent.parent / "context" / "starter_context.md"
    with open(context_path, 'r') as f:
        _starter_prompt = f.read()
    return _starter_prompt


def _attach_starter_audio_url(result: dict, audio_key: str) -> None:
    """Attach a presigned S3 URL for starter audio to the result dict."""
    bucket = os.environ.get('INSIGHTS_AUDIO_BUCKET')
    if not bucket:
        return
    s3 = _get_s3_client()
    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': audio_key},
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
        )
        result['audioUrl'] = url
        result['audioUrlExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=PRESIGNED_URL_EXPIRY_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception as e:
        logger.warning(f"Failed to generate presigned URL for starter audio {audio_key}: {e}")


def _invoke_generate_starter_audio(user_id: str) -> None:
    """Async self-invoke to generate TTS audio for cached starter insight."""
    function_name = os.environ.get('INSIGHTS_FUNCTION_NAME')
    if not function_name:
        logger.warning("INSIGHTS_FUNCTION_NAME not set, skipping starter TTS generation")
        return

    try:
        lambda_client = boto3.client('lambda')
        payload = json.dumps({
            'invocationType': 'GENERATE_STARTER_AUDIO',
            'userId': user_id,
        })
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=payload,
        )
        logger.info(f"Async-invoked starter TTS generation for user {user_id}")
    except Exception as e:
        logger.warning(f"Failed to invoke starter TTS generation: {e}")


def get_starter_insight(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get the one-time starter insight for a user.

    No premium check — available to all users. Cache-based dedup ensures
    one-time generation.
    """
    # Check cache first
    cached = get_cached_starter(user_id)
    if cached:
        body = cached.get('body', '')
        audio_key = cached.get('audioKey')
        result = {"body": body, "generatedAt": cached.get('generatedAt')}
        if audio_key:
            _attach_starter_audio_url(result, audio_key)
        return create_response(200, result)

    # No cache — generate synchronously
    from utils.openai_client import generate_starter_insight

    starter_prompt = _get_starter_prompt()
    curated = curate_starter_data(user_id)

    # If no tier unlocked (all 5 exercises not logged), return empty
    if curated is None:
        return create_response(200, {"body": None, "message": "No tier unlocked yet"})

    body = generate_starter_insight(starter_prompt, curated)
    model = os.environ.get('OPENAI_MODEL', 'gpt-5.4')
    put_cached_starter(user_id, body, model)

    # Fire-and-forget TTS
    _invoke_generate_starter_audio(user_id)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return create_response(200, {"body": body, "generatedAt": now_utc})


# ===========================================================================
# Pathway 6: GENERATE_STARTER_AUDIO (async self-invoke)
# ===========================================================================

def generate_starter_audio(user_id: str) -> dict:
    """
    Generate TTS audio for cached starter insight and update the cache item.

    Called asynchronously after starter insight is generated and cached.
    """
    from utils.tts import _generate_one, _prepare_for_tts
    from utils.openai_client import _get_client

    cached = get_cached_starter(user_id)
    if not cached:
        logger.warning(f"No cached starter insight for user {user_id} — skipping TTS")
        return {"status": "skipped", "reason": "no_cache"}

    if cached.get('audioKey'):
        logger.info(f"Starter audio already exists for user {user_id} — skipping")
        return {"status": "skipped", "reason": "already_exists"}

    body = cached.get('body', '')
    if not body:
        return {"status": "skipped", "reason": "no_body"}

    try:
        client = _get_client()
        bucket = os.environ.get('INSIGHTS_AUDIO_BUCKET')
        if not bucket:
            raise ValueError("INSIGHTS_AUDIO_BUCKET environment variable not set")

        s3_key = f"{user_id}/starter/0.mp3"
        _generate_one(client, body, bucket, s3_key)
        update_starter_audio_key(user_id, s3_key)
        logger.info(f"Starter TTS audio generated and cached for user {user_id}")
        return {"status": "completed", "audioKey": s3_key}
    except Exception as e:
        logger.error(f"Starter TTS generation failed for user {user_id}: {e}")
        return {"status": "error", "error": str(e)}


# ===========================================================================
# Pathway 7: POST_TIER_UNLOCK (API Gateway POST /insights/tier-unlock)
# ===========================================================================

# Module-level cache for the tier unlock system prompt
_tier_unlock_prompt = None


def _get_tier_unlock_prompt() -> str:
    """Load the tier_unlock_context.md system prompt (cached across warm invocations)."""
    global _tier_unlock_prompt
    if _tier_unlock_prompt is not None:
        return _tier_unlock_prompt

    context_path = Path(__file__).parent.parent / "context" / "tier_unlock_context.md"
    with open(context_path, 'r') as f:
        _tier_unlock_prompt = f.read()
    return _tier_unlock_prompt


def _invoke_generate_tier_unlock_audio(user_id: str, tier_name: str) -> None:
    """Async self-invoke to generate TTS audio for cached tier unlock insight."""
    function_name = os.environ.get('INSIGHTS_FUNCTION_NAME')
    if not function_name:
        logger.warning("INSIGHTS_FUNCTION_NAME not set, skipping tier unlock TTS generation")
        return

    try:
        lambda_client = boto3.client('lambda')
        payload = json.dumps({
            'invocationType': 'GENERATE_TIER_UNLOCK_AUDIO',
            'userId': user_id,
            'tierName': tier_name,
        })
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=payload,
        )
        logger.info(f"Async-invoked tier unlock TTS generation for user {user_id}, tier {tier_name}")
    except Exception as e:
        logger.warning(f"Failed to invoke tier unlock TTS generation: {e}")


def _attach_tier_audio_url(result: dict, audio_key: str) -> None:
    """Attach a presigned S3 URL for tier unlock audio to the result dict."""
    bucket = os.environ.get('INSIGHTS_AUDIO_BUCKET')
    if not bucket:
        return
    s3 = _get_s3_client()
    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': audio_key},
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
        )
        result['audioUrl'] = url
        result['audioUrlExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=PRESIGNED_URL_EXPIRY_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception as e:
        logger.warning(f"Failed to generate presigned URL for tier unlock audio {audio_key}: {e}")


def post_tier_unlock(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Generate or return cached tier unlock insight.

    No premium check — available to all users.
    """
    # Parse tier from request body
    try:
        body = json.loads(event.get('body', '{}'))
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {"error": "Invalid request body"})

    tier = body.get('tier')
    if not tier:
        return create_response(400, {"error": "Missing 'tier' in request body"})

    tier_lower = tier.lower()

    # Check cache — if exists, return it
    cached = get_cached_tier_unlock(user_id, tier_lower)
    if cached:
        result = {
            "tier": tier_lower,
            "body": cached.get('body', ''),
            "generatedAt": cached.get('generatedAt'),
        }
        audio_key = cached.get('audioKey')
        if audio_key:
            _attach_tier_audio_url(result, audio_key)
        return create_response(200, result)

    # Curate data (validates tier server-side, checks cache idempotency)
    from utils.openai_client import generate_tier_unlock_insight

    curated = curate_tier_unlock_data(user_id, tier)
    if curated is None:
        return create_response(200, {
            "tier": None,
            "body": None,
            "message": "No tier unlock message generated",
        })

    # Generate via GPT — adjust prompt based on premium status
    is_premium = check_premium(user_id)
    tier_unlock_prompt = _get_tier_unlock_prompt()
    if is_premium:
        tier_unlock_prompt = tier_unlock_prompt.replace(
            "{closing_weekly_narratives_mention}",
            "Close with a brief one-sentence mention encouraging the user to check out their weekly progress narratives for ongoing AI-powered analysis of their training."
        )
    else:
        tier_unlock_prompt = tier_unlock_prompt.replace(
            "{closing_weekly_narratives_mention}",
            "Close with a brief one-sentence mention encouraging the user to unlock weekly progress narratives for ongoing AI-powered analysis of their training."
        )
    body_text = generate_tier_unlock_insight(tier_unlock_prompt, curated)
    model = os.environ.get('OPENAI_MODEL', 'gpt-5.4')
    put_cached_tier_unlock(user_id, tier_lower, body_text, model)

    # Fire-and-forget TTS
    _invoke_generate_tier_unlock_audio(user_id, tier_lower)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return create_response(200, {
        "tier": tier_lower,
        "body": body_text,
        "generatedAt": now_utc,
    })


# ===========================================================================
# Pathway 8: GET_TIER_UNLOCKS (API Gateway GET /insights/tier-unlocks)
# ===========================================================================

def get_tier_unlocks(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get all tier unlock insights for a user.

    No premium check — available to all users. Includes lazy migration
    from starter → tier-novice.
    """
    items = get_all_tier_unlocks(user_id)

    tier_unlocks = []
    for item in items:
        sk = item.get('insightWeek', '')
        tier_name = sk.replace('tier-', '') if sk.startswith('tier-') else sk
        result = {
            "tier": tier_name,
            "body": item.get('body', ''),
            "generatedAt": item.get('generatedAt'),
        }
        audio_key = item.get('audioKey')
        if audio_key:
            _attach_tier_audio_url(result, audio_key)
        tier_unlocks.append(result)

    return create_response(200, {"tierUnlocks": tier_unlocks})


# ===========================================================================
# Pathway 9: GENERATE_TIER_UNLOCK_AUDIO (async self-invoke)
# ===========================================================================

def generate_tier_unlock_audio(user_id: str, tier_name: str) -> dict:
    """
    Generate TTS audio for cached tier unlock insight and update the cache item.

    Called asynchronously after tier unlock insight is generated and cached.
    """
    from utils.tts import _generate_one
    from utils.openai_client import _get_client

    cached = get_cached_tier_unlock(user_id, tier_name)
    if not cached:
        logger.warning(f"No cached tier unlock insight for user {user_id}, tier {tier_name} — skipping TTS")
        return {"status": "skipped", "reason": "no_cache"}

    if cached.get('audioKey'):
        logger.info(f"Tier unlock audio already exists for user {user_id}, tier {tier_name} — skipping")
        return {"status": "skipped", "reason": "already_exists"}

    body = cached.get('body', '')
    if not body:
        return {"status": "skipped", "reason": "no_body"}

    try:
        client = _get_client()
        bucket = os.environ.get('INSIGHTS_AUDIO_BUCKET')
        if not bucket:
            raise ValueError("INSIGHTS_AUDIO_BUCKET environment variable not set")

        s3_key = f"{user_id}/tier-{tier_name}/0.mp3"
        _generate_one(client, body, bucket, s3_key)
        update_tier_unlock_audio_key(user_id, tier_name, s3_key)
        logger.info(f"Tier unlock TTS audio generated for user {user_id}, tier {tier_name}")
        return {"status": "completed", "audioKey": s3_key}
    except Exception as e:
        logger.error(f"Tier unlock TTS generation failed for user {user_id}, tier {tier_name}: {e}")
        return {"status": "error", "error": str(e)}


# ===========================================================================
# Main Handler
# ===========================================================================

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for insights service.

    Routes based on invocation type:
    - EventBridge cron → PROCESS_TASKS
    - Async self-invoke → GENERATE_AUDIO
    - Async invoke from checkin → SCHEDULE_TASK
    - API Gateway → GET /insights/weekly
    """
    invocation_type = event.get("invocationType")

    # EventBridge cron
    if invocation_type == "PROCESS_TASKS":
        logger.info("Processing ripe insight tasks (EventBridge cron)")
        return process_ripe_tasks()

    # Async self-invoke to generate TTS audio
    if invocation_type == "GENERATE_AUDIO":
        user_id = event.get("userId")
        week_start = event.get("weekStart")
        if not user_id or not week_start:
            logger.error(f"GENERATE_AUDIO missing required fields: {event}")
            return {"error": "Missing userId or weekStart"}
        logger.info(f"Generating TTS audio for user {user_id}, week {week_start}")
        return generate_audio(user_id, week_start)

    # Async self-invoke to generate TTS audio for starter insight
    if invocation_type == "GENERATE_STARTER_AUDIO":
        user_id = event.get("userId")
        if not user_id:
            logger.error(f"GENERATE_STARTER_AUDIO missing userId: {event}")
            return {"error": "Missing userId"}
        logger.info(f"Generating starter TTS audio for user {user_id}")
        return generate_starter_audio(user_id)

    # Async self-invoke to generate TTS audio for tier unlock insight
    if invocation_type == "GENERATE_TIER_UNLOCK_AUDIO":
        user_id = event.get("userId")
        tier_name = event.get("tierName")
        if not user_id or not tier_name:
            logger.error(f"GENERATE_TIER_UNLOCK_AUDIO missing required fields: {event}")
            return {"error": "Missing userId or tierName"}
        logger.info(f"Generating tier unlock TTS audio for user {user_id}, tier {tier_name}")
        return generate_tier_unlock_audio(user_id, tier_name)

    # Async invoke from checkin Lambda
    if invocation_type == "SCHEDULE_TASK":
        user_id = event.get("userId")
        tz = event.get("createdTimezone")
        created_dt = event.get("createdDatetime")
        if not user_id or not tz or not created_dt:
            logger.error(f"SCHEDULE_TASK missing required fields: {event}")
            return {"error": "Missing userId, createdTimezone, or createdDatetime"}
        logger.info(f"Scheduling insight task for user {user_id}")
        return schedule_insight_task(user_id, tz, created_dt)

    # API Gateway (GET /insights/weekly)
    http_method = event.get("httpMethod")
    path = event.get("path", "")

    if http_method and event.get("requestContext", {}).get("authorizer"):
        user_id = event["requestContext"]["authorizer"]["userId"]
        set_sentry_user(user_id)
        logger.info(f"Insights request: {http_method} {path} for user {user_id}")

        if http_method == "POST" and path.endswith("/insights/tier-unlock"):
            return post_tier_unlock(event, user_id)

        if http_method == "GET" and path.endswith("/insights/tier-unlocks"):
            return get_tier_unlocks(event, user_id)

        if http_method == "GET" and path.endswith("/insights/starter"):
            return get_starter_insight(event, user_id)

        if http_method == "GET" and path.endswith("/insights/weekly"):
            return get_weekly_insights(event, user_id)

    return create_response(404, {"error": f"Route not found: {event.get('httpMethod')} {event.get('path', '')}"})
