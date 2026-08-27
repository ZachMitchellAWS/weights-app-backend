"""
Insights service Lambda handler.

Serves the tier-unlock insight — the short generated narrative and audio clip the app shows on the
Strength tab after a user reaches a new overall strength tier.

Five invocation pathways:
1. POST_TIER_UNLOCK — API Gateway POST /insights/tier-unlock
2. GET_TIER_UNLOCKS — API Gateway GET /insights/tier-unlocks
3. GENERATE_TIER_UNLOCK_AUDIO — async self-invoke, TTS for a tier unlock
4. GET_STARTER_INSIGHT / GENERATE_STARTER_AUDIO — the superseded starter insight. No client calls
   it; kept because reading it lazily migrates a legacy starter row into a tier-unlock row.
5. PROCESS_TASKS — EventBridge cron, now a no-op (see `handler`).

This service also generated Weekly Progress Narratives, which Smart Sessions replaced. That path
is gone: `GET /insights/weekly`, the task queue, and the checkin Lambda's scheduling invoke. The
`insight-tasks` table and the 15-minute cron rule were left standing rather than torn down, so no
data was destroyed — they simply have nothing to do.
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
from utils.cache import (
    get_cached_starter, put_cached_starter, update_starter_audio_key,
    get_cached_tier_unlock, put_cached_tier_unlock, get_all_tier_unlocks,
    update_tier_unlock_audio_key,
)
from utils.data_curator import curate_starter_data, curate_tier_unlock_data
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


# ===========================================================================
# Pathway 1: SCHEDULE_TASK (async invoke from checkin Lambda)
# ===========================================================================

# ===========================================================================
# Pathway 2: PROCESS_TASKS (EventBridge cron)
# ===========================================================================

# ===========================================================================
# Pathway 3: GENERATE_AUDIO (async self-invoke)
# ===========================================================================

# ===========================================================================
# Pathway 4: GET_INSIGHTS (API Gateway)
# ===========================================================================

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

    # Generate via GPT — adjust the closing line based on premium status.
    #
    # This used to promote Weekly Progress Narratives, which Smart Sessions replaced. The
    # placeholder name is kept because it appears twice in tier_unlock_context.md and renaming it
    # buys nothing; what it substitutes is what matters.
    #
    # NOTE: tier-unlock rows are cached with no TTL and returned before regeneration, so editing
    # this only affects users who have not yet unlocked the tier in question. Everyone who already
    # has one keeps the old closing permanently.
    is_premium = check_premium(user_id)
    tier_unlock_prompt = _get_tier_unlock_prompt()
    if is_premium:
        tier_unlock_prompt = tier_unlock_prompt.replace(
            "{closing_weekly_narratives_mention}",
            "Close with a brief one-sentence mention encouraging the user to start a Smart Session "
            "on the Session tab, which builds them a session around the time they have and how "
            "they are feeling that day."
        )
    else:
        tier_unlock_prompt = tier_unlock_prompt.replace(
            "{closing_weekly_narratives_mention}",
            "Close with a brief one-sentence mention encouraging the user to unlock Smart Sessions, "
            "which builds them a session around the time they have and how they are feeling that day."
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
    - Async self-invoke → GENERATE_STARTER_AUDIO / GENERATE_TIER_UNLOCK_AUDIO
    - API Gateway → POST /insights/tier-unlock, GET /insights/tier-unlocks, GET /insights/starter
    - EventBridge cron → PROCESS_TASKS, now a no-op (see below)
    """
    invocation_type = event.get("invocationType")

    # EventBridge cron — DORMANT.
    #
    # This drove Weekly Progress Narratives generation, which Smart Sessions replaced. The rule and
    # the insight-tasks table were deliberately left in place rather than torn down, so this fires
    # every 15 minutes with nothing to do. Answered explicitly: without this branch it falls
    # through to the 404 at the bottom and logs "Route not found: None None" forever.
    if invocation_type == "PROCESS_TASKS":
        return {"status": "noop", "reason": "weekly narratives removed"}

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

    # API Gateway — tier-unlock pair plus the dormant starter route
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

    return create_response(404, {"error": f"Route not found: {event.get('httpMethod')} {event.get('path', '')}"})
