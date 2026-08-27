"""Persist one record per generation request, and count moderation violations.

Both live in the sessions service's own table rather than anywhere shared. The violation
counter in particular was originally going to sit on user-properties, which could not work:
`GET /user/properties` returns the whole item verbatim, so the count would have shipped to
the client on the next fetch — the one thing it must never do.

NOTHING HERE MAY COST THE USER A SESSION. A generation takes ~20 seconds and sits against a
hard 29-second ceiling; a slow or failing write at the end of that must never turn a finished
session into an error. So every call is bounded, wrapped, and swallowed.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Aggressive, deliberately. boto3 defaults to a 60s connect and three retries — on a
# DynamoDB brownout that would eat the entire response reserve and get the Lambda killed
# holding a session it had already generated.
_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 1, "mode": "standard"})
_dynamodb = boto3.resource("dynamodb", config=_CONFIG)

# Below this much Lambda time left, skip the write entirely. Returning the session matters
# more than recording it.
MIN_REMAINING_MS = 2000

# Sort key for the per-user aggregate. The "#" prefix keeps it out of the way of the uuid
# sort keys used by the request records.
VIOLATIONS_SK = "#violations"


def _table():
    name = os.environ.get("GENERATED_SESSIONS_TABLE_NAME")
    if not name:
        raise ValueError("GENERATED_SESSIONS_TABLE_NAME environment variable not set")
    return _dynamodb.Table(name)


def _remaining_ms(context) -> int:
    try:
        return int(context.get_remaining_time_in_millis())
    except Exception:
        return MIN_REMAINING_MS


def record(context, *, user_id, chips, note, note_used, moderation_status,
           moderation_categories, outcome, duration_ms, model, session=None) -> None:
    """Write one request record. Never raises.

    `note` is stored RAW and always — including when moderation rejected it. That is the
    whole point of the record: a count of violations without the text behind it cannot tell
    a genuine abuse pattern from a filter that is too aggressive.
    """
    if _remaining_ms(context) < MIN_REMAINING_MS:
        logger.warning("Skipping session record: %dms left", _remaining_ms(context))
        return

    item = {
        "userId": user_id,
        "sessionId": str(uuid.uuid4()),
        "createdDatetime": datetime.now(timezone.utc).isoformat(),
        "chips": chips,
        "note": note,
        "noteUsed": note_used,
        "moderationStatus": moderation_status,
        "moderationCategories": moderation_categories,
        # `outcome`, not `status` — and `modelResponse`, not `items`. Both of those, plus
        # `duration` and `session`, are DynamoDB reserved words. PutItem uses no expression
        # so they would work today and fail the first time anyone writes a query.
        "outcome": outcome,
        "durationMs": duration_ms,
        "model": model,
    }
    if session:
        item["modelResponse"] = {
            "summary": session.get("summary", ""),
            "lifts": session.get("items", []),
        }

    try:
        _table().put_item(Item=item)
    except Exception:
        logger.exception("Failed to write session record (session still returned)")


def increment_violation(context, user_id: str) -> None:
    """Bump the per-user abuse counter. Never raises.

    Callers must gate this on `moderation.counts_as_violation` — an `error` verdict is our
    failure, not the user's, and a self-harm flag is not abuse.
    """
    if _remaining_ms(context) < MIN_REMAINING_MS:
        return

    try:
        _table().update_item(
            Key={"userId": user_id, "sessionId": VIOLATIONS_SK},
            # ADD treats a missing attribute as zero, so the first violation needs no read
            # and no create — and two concurrent requests cannot lose an increment.
            UpdateExpression="ADD #c :one SET #last = :now",
            ExpressionAttributeNames={"#c": "violationCount", "#last": "lastViolationAt"},
            ExpressionAttributeValues={":one": 1, ":now": datetime.now(timezone.utc).isoformat()},
            ReturnValues="NONE",
        )
    except ClientError:
        logger.exception("Failed to increment violation counter")
    except Exception:
        logger.exception("Failed to increment violation counter")
