"""Session generation handler.

POST /sessions/generate — premium-gated, synchronous.

The client sends only what the backend cannot know: the set plan catalog (which lives in
client code and includes plans the user created) and their free-text context. Everything
else is assembled from DynamoDB by `payload_builder`.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

from utils import moderation, session_record
from utils.entitlement_check import check_premium
from utils.openai_client import DEFAULT_DEADLINE_SECONDS, GenerationTimeout, generate_session
from utils import payload_builder
from utils.payload_builder import build_payload
from utils.response import create_response
from utils.sentry_init import init_sentry, set_sentry_user

logger = logging.getLogger()
logger.setLevel(logging.INFO)

init_sentry()

_PROMPT_PATH = Path(__file__).parent.parent / "context" / "session_generation.md"
_system_prompt: str | None = None


def _load_system_prompt() -> str:
    """Read the prompt once per warm container."""
    global _system_prompt
    if _system_prompt is None:
        _system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt


# The app persists "redline" and "pr" in a set plan's effortSequence; the payload speaks the
# words the product says out loud. Accepted on input either way so a client sending its stored
# spellings does not put a word the prompt never defined in front of the model.
_EFFORT_ALIASES = {"redline": "near_max", "pr": "progress"}

# Bounds on what the user may hand the generator. The client enforces both and shows the
# user where they stand; these are the backstop, because a client is not a validator.
#
# Over-length notes are TRUNCATED, not rejected. Someone who types 520 characters meant all
# of it, and failing their request over 20 characters helps nobody — the first 500 carry the
# intent.
NOTE_CHAR_LIMIT = 500
MAX_CHIPS = 8


def _validate_request(body: dict) -> tuple[list[dict], dict] | None:
    """Pull the catalog and context out of the request body, or None if unusable."""
    catalog = body.get("set_plan_catalog")
    if not isinstance(catalog, list) or not catalog:
        return None

    cleaned = []
    for plan in catalog:
        if not isinstance(plan, dict):
            return None
        if not plan.get("id") or not plan.get("name") or not isinstance(plan.get("sequence"), list):
            return None
        sequence = [
            _EFFORT_ALIASES.get(str(e).lower(), str(e).lower())
            for e in plan["sequence"]
        ]
        cleaned.append({
            "id": plan["id"],
            "name": plan["name"],
            "sequence": sequence,
            "description": plan.get("description", ""),
        })

    raw_context = body.get("user_context") or {}
    chips = [c for c in (raw_context.get("chips") or []) if isinstance(c, str)]
    note = raw_context.get("note") or ""
    if not isinstance(note, str):
        note = ""

    context = {
        "chips": chips[:MAX_CHIPS],
        "note": note[:NOTE_CHAR_LIMIT],
    }
    return cleaned, context


def _validate_response(session: dict, valid_exercise_ids: set, valid_plan_ids: set) -> bool:
    """Every returned id must be one we sent.

    A schema guarantees the shape of the output, never its truthfulness — the model can
    return a well-formed uuid that refers to nothing. An unresolvable id would surface on
    the client as a session item that silently does nothing, so the whole response is
    rejected rather than partially trusted.

    An EMPTY items list is valid and is not checked here. The prompt tells the model never
    to prescribe work already done today, so a user who has trained all five lifts gets a
    correct answer of "nothing". This function used to reject that as malformed, which
    turned the most reasonable possible response into a 502 — see `generate`, which now
    reports it as its own outcome.
    """
    items = session.get("items")
    if not isinstance(items, list):
        return False

    for item in items:
        if item.get("exercise_id") not in valid_exercise_ids:
            logger.warning("Model returned unknown exercise_id: %s", item.get("exercise_id"))
            return False
        if item.get("set_plan_id") not in valid_plan_ids:
            logger.warning("Model returned unknown set_plan_id: %s", item.get("set_plan_id"))
            return False
    return True


def _is_staging() -> bool:
    return os.environ.get("ENVIRONMENT") == "staging"


def _premium_ok(user_id: str) -> bool:
    """Premium gate, waived in staging.

    Staging has no App Store sandbox subscriptions behind most test accounts, and the
    client's `PremiumOverride` only flips its own local state — the backend would still
    return 402 and the feature would be untestable end to end without buying something.

    Fails CLOSED in production: this reads the environment the stack itself injected, so
    a missing or unexpected value gates rather than opens.
    """
    if _is_staging():
        if not check_premium(user_id):
            logger.info("Staging: premium gate waived for user %s", user_id)
        return True
    return check_premium(user_id)


# Time reserved AFTER the model call returns: validating ids, building the response, writing
# the record, and letting Sentry flush. Small, but it is the difference between a structured
# 503 the client can retry and the Lambda being killed mid-sentence.
#
# 5.0 rather than 4.0 since the record write joined the tail of the request. Moderation does
# NOT need its own allowance here: it runs before `_deadline_from` is called, so the time it
# spends is already gone from `get_remaining_time_in_millis()` and generation's budget
# shrinks to match automatically.
RESPONSE_RESERVE_SECONDS = 5.0


def _deadline_from(context: Any) -> float:
    """How long the model call may run, from the Lambda's own remaining time.

    Derived rather than hardcoded because everything before this point — cold start, SSM
    fetch, four DynamoDB queries — spends from the same budget. A fixed constant cannot see
    any of that, which is how a "20 second" cap ended up overrunning a 28 second function.
    """
    try:
        remaining = context.get_remaining_time_in_millis() / 1000.0
    except Exception:
        return DEFAULT_DEADLINE_SECONDS
    return remaining - RESPONSE_RESERVE_SECONDS


def _finalise(context, user_id, chips, note, note_used, mod_status, mod_categories,
              outcome, elapsed, session) -> None:
    """Record the request and, if warranted, count it against the user.

    One function so that no exit path can quietly skip it — an `outcome` field is worthless
    if only the success path ever writes one. Both calls swallow their own failures, so this
    can never cost the caller its response.
    """
    session_record.record(
        context,
        user_id=user_id,
        chips=chips,
        note=note,
        note_used=note_used,
        moderation_status=mod_status,
        moderation_categories=mod_categories,
        outcome=outcome,
        duration_ms=int(elapsed * 1000),
        model=os.environ.get("OPENAI_MODEL", "gpt-5.4"),
        session=session,
    )
    if moderation.counts_as_violation(mod_status):
        session_record.increment_violation(context, user_id)


def generate(event: Dict[str, Any], user_id: str, context: Any) -> Dict[str, Any]:
    if not _premium_ok(user_id):
        return create_response(402, {
            "error": "Premium required",
            "message": "Session generation requires an active subscription",
        })

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return create_response(400, {"error": "Invalid JSON", "message": "Request body is not valid JSON"})

    parsed = _validate_request(body)
    if parsed is None:
        return create_response(400, {
            "error": "Invalid request",
            "message": "set_plan_catalog must be a non-empty list of {id, name, sequence}",
        })
    catalog, user_context = parsed

    # Screened BEFORE the payload is built, so flagged text never enters it at all.
    #
    # Placed here rather than later for a second reason: `_deadline_from` reads the Lambda's
    # REMAINING time, so whatever moderation spends is already subtracted from generation's
    # budget by the time that is computed. No manual arithmetic, no double-spending.
    note_allowed, mod_categories, mod_status = moderation.screen(user_context["note"])
    raw_note = user_context["note"]
    if not note_allowed:
        # Blanked, not annotated. Telling the model something was removed only invites it to
        # speculate about what.
        user_context = dict(user_context, note="")

    try:
        payload = build_payload(user_id, catalog, user_context)
    except Exception as e:
        logger.exception("Failed to assemble payload")
        # Deliberately not `str(e)`. The exception can carry fragments of the request,
        # including the user's note, and this string goes to the client.
        return create_response(500, {
            "error": "Payload error",
            "message": "Could not assemble training data for this request",
        })

    lifts = payload.get("strength", {}).get("lifts", {})
    if not lifts:
        return create_response(422, {
            "error": "Insufficient data",
            "message": "No fundamental lifts found for this user",
        })

    if _is_staging():
        # The payload is the whole product. Logged in staging so it can be diffed against
        # SESSION_GENERATION_INPUTS.md by eye.
        logger.info("Session payload: %s", json.dumps(payload, default=str))

    started = time.time()
    try:
        session = generate_session(
            _load_system_prompt(),
            json.dumps(payload, default=str),
            deadline_seconds=_deadline_from(context),
        )
    except GenerationTimeout as e:
        # Expected under load, not an incident. Logged without a stack trace so a slow model
        # does not read as a crash in Sentry, and returned as the same retryable 503 the
        # client already handles.
        elapsed = time.time() - started
        logger.warning("Generation timed out after %.1fs: %s", elapsed, e)
        _finalise(context, user_id, user_context["chips"], raw_note, note_allowed,
                  mod_status, mod_categories, "timeout", elapsed, None)
        return create_response(503, {
            "error": "Generation timed out",
            "message": "Session generation took too long",
            "retryable": True,
        })
    except Exception as e:
        elapsed = time.time() - started
        logger.exception("Generation failed after %.1fs", elapsed)
        _finalise(context, user_id, user_context["chips"], raw_note, note_allowed,
                  mod_status, mod_categories, "failed", elapsed, None)
        # Retryable on purpose: the client's Retry button re-requests from scratch, which is
        # the retry strategy for this endpoint — see openai_client on why there is no loop.
        return create_response(503, {
            "error": "Generation failed",
            "message": str(e),
            "retryable": True,
        })

    elapsed = time.time() - started
    # Instrumented from day one: this number is what decides whether the synchronous design
    # survives contact with the 29s ceiling.
    logger.info("Session generation took %.1fs", elapsed)

    valid_exercise_ids = {l["id"] for l in lifts.values()}
    valid_plan_ids = {p["id"] for p in catalog}
    if not _validate_response(session, valid_exercise_ids, valid_plan_ids):
        _finalise(context, user_id, user_context["chips"], raw_note, note_allowed,
                  mod_status, mod_categories, "invalid", elapsed, session)
        return create_response(502, {
            "error": "Invalid generation",
            "message": "Model returned a session referencing unknown ids",
        })

    # "Nothing left to do today" is an answer, not a failure. Flagged explicitly rather
    # than left for the client to infer from an empty array, so the two cases the client
    # must tell apart — no work to recommend, versus items it could not resolve locally —
    # never depend on the same emptiness check.
    #
    # But it is only allowed when the data says so. The model reads `today_coverage`, and
    # this re-checks the same computed verdict rather than trusting that it did: an empty
    # session is the one response a user cannot act on, so it should not be reachable by
    # the model deciding on its own that a day looks finished.
    if not session.get("items"):
        coverage = payload.get("today_coverage", {})
        if payload_builder.all_covered(coverage):
            logger.info("Session generation returned no lifts for user %s (all lifts covered)", user_id)
            _finalise(context, user_id, user_context["chips"], raw_note, note_allowed,
                      mod_status, mod_categories, "nothing_to_recommend", elapsed, session)
            return create_response(200, {
                "session": session,
                "nothing_to_recommend": True,
                "note_used": note_allowed,
            })

        open_lifts = [name for name, lift in coverage.items() if not lift["covered"]]
        logger.warning(
            "Model returned an empty session for user %s with %d lifts still open: %s",
            user_id, len(open_lifts), ", ".join(open_lifts),
        )
        _finalise(context, user_id, user_context["chips"], raw_note, note_allowed,
                  mod_status, mod_categories, "invalid", elapsed, session)
        return create_response(502, {
            "error": "Invalid generation",
            "message": "Model returned no lifts while work remains for today",
        })

    _finalise(context, user_id, user_context["chips"], raw_note, note_allowed,
              mod_status, mod_categories, "ok", elapsed, session)

    # One boolean, no reason code. "Flagged" and "we could not check" are both "couldn't be
    # used" to the user, and a reason code would only tell someone probing the filter which
    # of the two they hit.
    return create_response(200, {"session": session, "note_used": note_allowed})


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    http_method = event.get("httpMethod")
    path = event.get("path", "")

    authorizer = event.get("requestContext", {}).get("authorizer") or {}
    user_id = authorizer.get("userId") or authorizer.get("principalId")

    if not user_id:
        return create_response(401, {"error": "Unauthorized", "message": "Missing user context"})

    set_sentry_user(user_id)
    logger.info("Sessions request: %s %s for user %s", http_method, path, user_id)

    if http_method == "POST" and path.endswith("/sessions/generate"):
        return generate(event, user_id, context)

    return create_response(404, {"error": "Not found", "message": f"No route for {http_method} {path}"})
