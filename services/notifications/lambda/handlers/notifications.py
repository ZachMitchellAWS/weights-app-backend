"""Notifications service Lambda handler.

Sends APNs pushes, either on a schedule or on demand. Four invocation pathways:

1. SCHEDULE_FANOUT       — EventBridge cron, every 15 minutes. Splits the shard space and
                           self-invokes one worker per range. Sends nothing itself.
2. PROCESS_SHARD_RANGE   — async self-invoke. Owns a contiguous shard range: finds ripe tasks,
                           claims, sends, logs, deletes.
3. SEND_NOW              — async self-invoke or direct invoke from another service. One
                           notification, immediately (or after `delaySeconds`). No task row.
4. API Gateway           — POST /notifications/test, STAGING ONLY. The route does not exist in
                           production: `notifications_stack.py` only creates it when
                           env == "staging", so this is not merely hidden behind a client flag.

WHY FAN-OUT RATHER THAN ONE BIG LOOP. The cron carries a `concurrency` value, and the shard space
divides by it. Today it is 1 and one worker sweeps all 100 shards in well under a second. If
volume ever justifies it, raising the number in `config/` splits the same work across more
concurrent workers with no code change and no risk of two workers seeing the same task — the
ranges are disjoint by construction.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

import boto3

from utils import apns, notification_log as nlog, notification_types, tasks, tokens
from utils.response import create_response
from utils.sentry_init import init_sentry, set_sentry_user

init_sentry()

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda_client = None

# Guard on the dev pathway. `delaySeconds` exists so a button press can be observed arriving
# rather than racing the UI back to the foreground; it is not a scheduling mechanism, and it
# burns Lambda wall-clock, so it is capped hard.
MAX_DELAY_SECONDS = 30

# Per-shard page size. A shard holding more than this many ripe tasks is swept again on the next
# tick rather than in a loop here — bounded work per invocation keeps one hot shard from pushing
# the whole worker past its timeout.
MAX_TASKS_PER_SHARD = 100


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _self_invoke(payload: dict) -> None:
    """Fire-and-forget self-invocation. `Event` so the caller does not wait — the fan-out
    must not hold one invocation open for the lifetime of all its workers."""
    _lambda().invoke(
        FunctionName=os.environ["SELF_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


# --------------------------------------------------------------------------- #
# Pathway 1: SCHEDULE_FANOUT
# --------------------------------------------------------------------------- #
def schedule_fanout(event: Dict[str, Any]) -> Dict[str, Any]:
    """Divide the shard space and hand each range to its own worker."""
    concurrency = int(event.get("concurrency") or 1)
    ranges = tasks.shard_ranges(concurrency)
    for start, end in ranges:
        _self_invoke({
            "invocationType": "PROCESS_SHARD_RANGE",
            "shardStart": start,
            "shardEnd": end,
        })
    logger.info("Fanned out %d worker(s) over %d shards", len(ranges), tasks.SHARD_COUNT)
    return {"status": "ok", "workers": len(ranges), "ranges": ranges}


# --------------------------------------------------------------------------- #
# Pathway 2: PROCESS_SHARD_RANGE
# --------------------------------------------------------------------------- #
def process_shard_range(event: Dict[str, Any]) -> Dict[str, Any]:
    """Sweep every shard in this worker's range."""
    start = int(event["shardStart"])
    end = int(event["shardEnd"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    counts = {"claimed": 0, "delivered": 0, "skipped": 0, "failed": 0}
    for shard in range(start, end + 1):
        for task in tasks.ripe_tasks(shard, now, limit=MAX_TASKS_PER_SHARD):
            claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if not tasks.claim_task(shard, task["dueAtTaskId"], now):
                # Someone else has it, or it is mid-flight elsewhere. Not an error.
                continue
            counts["claimed"] += 1
            # Isolate each task. An unhandled exception here used to abort the entire range,
            # so a single malformed row could stall every shard behind it — and it did: a
            # reserved-keyword bug in the claim took out the whole sweep rather than one task.
            # The claim is left in place on an unexpected error so the stale-recovery path
            # retries it rather than hot-looping every 15 minutes.
            try:
                outcome = _deliver_task(task, claimed_at)
            except Exception:  # noqa: BLE001 - one task must not stop the others
                logger.exception("Task %s failed unexpectedly", task.get("taskId"))
                counts["failed"] += 1
                continue
            if outcome == nlog.DELIVERED:
                counts["delivered"] += 1
            elif outcome == nlog.FAILED_TRANSIENT:
                counts["failed"] += 1
            else:
                counts["skipped"] += 1

    logger.info("Shards %d-%d: %s", start, end, counts)
    return {"status": "ok", "shardStart": start, "shardEnd": end, **counts}


def _deliver_task(task: Dict[str, Any], claimed_at: str) -> str:
    """Run one task to a terminal outcome, then tidy up after it.

    Terminal outcomes delete the task. A transient failure releases the claim instead, so the
    next sweep retries it — the distinction is the difference between "this will never work" and
    "Apple was busy".
    """
    shard = task["shard"]
    key = task["dueAtTaskId"]
    outcome = send_notification(
        user_id=task["userId"],
        notification_type=task["notificationType"],
        task_id=task.get("taskId"),
        pathway="scheduled",
        # `is True` rather than truthiness, matching the convention in tokens.py — a stray
        # string on a hand-written task must not silently enable this.
        bypass_precondition=task.get("bypassPrecondition") is True,
    )

    if outcome == nlog.FAILED_TRANSIENT:
        tasks.release_task(shard, key)
    else:
        tasks.delete_task(shard, key, claimed_at)
    return outcome


# --------------------------------------------------------------------------- #
# Pathway 3: SEND_NOW  (and the shared send path for everything above)
# --------------------------------------------------------------------------- #
def send_notification(user_id: str, notification_type: str, *,
                      task_id: str | None = None, pathway: str = "direct",
                      bypass_precondition: bool = False) -> str:
    """Resolve a type, check its precondition, and push to every usable token.

    Returns the outcome that should decide the task's fate. Every branch logs before returning —
    a send that does nothing is exactly as interesting as one that succeeds.

    `bypass_precondition` is a TEST AFFORDANCE. Once an account has done the thing a
    notification asks for, that notification can never be delivered to it again — which is
    correct for users and useless for testing against a real device. The flag is opt-in per
    send (set on the task item, or in a SEND_NOW payload); nothing sets it in bulk.

    It deliberately does NOT relax the token checks. "Does this user still need it" is a
    product judgement worth overriding in a test; "can we reach this device" is a fact, and
    overriding it would just produce a confusing failure further down.
    """
    set_sentry_user(user_id)

    spec = notification_types.get(notification_type)
    if spec is None:
        # A hand-written row or a type deleted while tasks referencing it were still queued.
        # Drop it rather than raising, so one bad row cannot stall a shard forever.
        logger.warning("Unknown notification type %r for user %s", notification_type, user_id)
        nlog.record(user_id, notification_type, nlog.SKIPPED_UNKNOWN_TYPE,
                    task_id=task_id, pathway=pathway)
        return nlog.SKIPPED_UNKNOWN_TYPE

    properties = tokens.user_properties(user_id)
    ctx = notification_types.RenderContext(user_id=user_id, user_properties=properties)

    # The world moved between queueing and sending. Nudging someone to do a thing they have
    # since done is worse than staying quiet.
    if not spec.precondition(ctx):
        if not bypass_precondition:
            nlog.record(user_id, notification_type, nlog.SKIPPED_PRECONDITION,
                        task_id=task_id, pathway=pathway)
            return nlog.SKIPPED_PRECONDITION
        # Recorded loudly on both sides: a warning here, and a `-bypass` suffix on the log
        # row's pathway. A `delivered` row that quietly skipped its precondition would make
        # the notification log stop meaning what it says.
        logger.warning("Precondition BYPASSED for user %s / %s", user_id, notification_type)
        pathway = f"{pathway}-bypass"

    usable = tokens.usable_tokens(user_id, properties=properties)
    if not usable:
        nlog.record(user_id, notification_type, nlog.SKIPPED_NO_TOKEN,
                    task_id=task_id, pathway=pathway)
        return nlog.SKIPPED_NO_TOKEN

    # One user can have several devices. The task's fate follows the BEST outcome across them:
    # one delivered phone means the notification happened, and a second dead token should not
    # cause the whole thing to be retried.
    outcomes = []
    for row in usable:
        outcomes.append(_send_to_token(row, spec, user_id, task_id, pathway))

    if nlog.DELIVERED in outcomes:
        return nlog.DELIVERED
    if nlog.FAILED_TRANSIENT in outcomes:
        return nlog.FAILED_TRANSIENT
    return outcomes[0]


def _send_to_token(row: Dict[str, Any], spec, user_id: str,
                   task_id: str | None, pathway: str) -> str:
    device_token = row["apnsToken"]
    environment = row.get("apnsEnvironment") or apns.ENV_PRODUCTION
    was_fallback = bool(row.get("_fallback"))

    result = apns.send(
        device_token=device_token,
        title=spec.title,
        body=spec.body,
        environment=environment,
        category=spec.category,
        # Collapsing on the type means a user who somehow accrues two of the same nudge sees
        # one notification, not a stack.
        collapse_id=spec.key,
    )

    if result.ok:
        tokens.record_delivery(user_id, device_token, result.environment,
                               was_fallback=was_fallback)
        # The send proved which host answers for this token; correcting it now saves every
        # future send the retry round trip.
        if result.environment != environment:
            tokens.correct_environment(user_id, device_token, result.environment)
        outcome = nlog.DELIVERED
    elif result.token_is_dead:
        tokens.mark_invalid(user_id, device_token, result.reason or f"status {result.status_code}")
        outcome = nlog.FAILED_INVALID_TOKEN
    else:
        outcome = nlog.FAILED_TRANSIENT

    nlog.record(user_id, spec.key, outcome,
                token_suffix=tokens.token_suffix(device_token),
                apns_status=result.status_code, apns_id=result.apns_id,
                apns_reason=result.reason, apns_environment=result.environment,
                task_id=task_id, pathway=pathway)
    return outcome


def send_now(event: Dict[str, Any]) -> Dict[str, Any]:
    user_id = event.get("userId")
    notification_type = event.get("notificationType")
    if not user_id or not notification_type:
        logger.error("SEND_NOW missing userId or notificationType: %s", event)
        return {"error": "Missing userId or notificationType"}

    delay = min(int(event.get("delaySeconds") or 0), MAX_DELAY_SECONDS)
    if delay > 0:
        time.sleep(delay)

    outcome = send_notification(
        user_id, notification_type, pathway="direct",
        bypass_precondition=event.get("bypassPrecondition") is True,
    )
    return {"status": "ok", "outcome": outcome}


# --------------------------------------------------------------------------- #
# Pathway 4: API Gateway — POST /notifications/test (staging only)
# --------------------------------------------------------------------------- #
def post_test(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Queue an immediate send to the caller's own account.

    Async self-invoke so the request returns at once and the delay is served off the request
    path. `userId` comes from the JWT claims and is never read from the body — a test endpoint
    that lets you push to an arbitrary account is not a test endpoint.
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        body = {}

    notification_type = body.get("notificationType") or "unlock-strength-tier-nudge"
    if notification_types.get(notification_type) is None:
        return create_response(status_code=400, body={
            "error": "Unknown notification type",
            "message": f"{notification_type} is not registered",
        })

    delay = min(int(body.get("delaySeconds") or 5), MAX_DELAY_SECONDS)
    _self_invoke({
        "invocationType": "SEND_NOW",
        "userId": user_id,
        "notificationType": notification_type,
        "delaySeconds": delay,
    })
    return create_response(status_code=202, body={
        "message": f"Queued {notification_type}, sending in {delay}s",
        "notificationType": notification_type,
        "delaySeconds": delay,
    })


# --------------------------------------------------------------------------- #
def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Route by invocation type, then by HTTP method for API Gateway events."""
    invocation_type = event.get("invocationType")

    if invocation_type == "SCHEDULE_FANOUT":
        return schedule_fanout(event)

    if invocation_type == "PROCESS_SHARD_RANGE":
        return process_shard_range(event)

    if invocation_type == "SEND_NOW":
        return send_now(event)

    http_method = event.get("httpMethod")
    path = event.get("path", "")
    if http_method and event.get("requestContext", {}).get("authorizer"):
        user_id = event["requestContext"]["authorizer"]["userId"]
        logger.info("Notifications request: %s %s for user %s", http_method, path, user_id)
        if http_method == "POST" and path.endswith("/notifications/test"):
            return post_test(event, user_id)

    logger.warning("Route not found: %s %s (invocationType=%s)",
                   http_method, path, invocation_type)
    return create_response(status_code=404, body={"error": "Not found"})
