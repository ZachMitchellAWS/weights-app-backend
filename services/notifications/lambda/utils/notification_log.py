"""The notification log — what we tried to send, and what happened.

EVERY ATTEMPT IS LOGGED, INCLUDING THE ONES THAT SENT NOTHING. The question this table exists to
answer is "why did this user not get a notification", and a table that only records successes
cannot answer it. A precondition that cancelled, a user with no usable token, a token Apple
rejected — each writes a row, and `outcome` is the field that distinguishes them.

NO FULL DEVICE TOKENS. Only `tokenSuffix`, the last 8 characters. A device token is a credential
for pushing to that device; this table is read casually during triage and should not carry one.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

# Outcomes. Deliberately distinguishes the three "nothing was sent" cases from each other,
# because they call for completely different responses: fix nothing, fix the token, fix the code.
DELIVERED = "delivered"
SKIPPED_PRECONDITION = "skipped_precondition"      # the user no longer needs this notification
SKIPPED_NO_TOKEN = "skipped_no_token"              # nobody to send to
SKIPPED_UNKNOWN_TYPE = "skipped_unknown_type"      # task references a type that is not registered
FAILED_INVALID_TOKEN = "failed_invalid_token"      # Apple says this token is dead
FAILED_TRANSIENT = "failed_transient"              # worth retrying on the next sweep

LOG_RETENTION_DAYS = 90

_dynamodb = None


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    name = os.environ.get("NOTIFICATION_LOG_TABLE_NAME")
    if not name:
        raise ValueError("NOTIFICATION_LOG_TABLE_NAME environment variable not set")
    return _dynamodb.Table(name)


def record(user_id: str, notification_type: str, outcome: str, *,
           token_suffix: str | None = None, apns_status: int | None = None,
           apns_id: str | None = None, apns_reason: str | None = None,
           apns_environment: str | None = None, task_id: str | None = None,
           pathway: str | None = None) -> None:
    """Write one attempt.

    Never raises: a failure to log must not fail a send that already happened, nor stall a shard
    on its way through a batch. The exception is swallowed here rather than at every call site.
    """
    now = datetime.now(timezone.utc)
    item: dict[str, Any] = {
        "userId": user_id,
        "sentAtTaskId": f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}#{uuid.uuid4()}",
        "notificationType": notification_type,
        "outcome": outcome,
        "ttl": int((now + timedelta(days=LOG_RETENTION_DAYS)).timestamp()),
    }
    # Only set what actually applies — a skipped-precondition row has no APNs fields, and
    # writing empty strings for them would make the table harder to read, not easier.
    for key, value in (
        ("tokenSuffix", token_suffix),
        ("apnsStatusCode", apns_status),
        ("apnsId", apns_id),
        ("apnsReason", apns_reason),
        ("apnsEnvironment", apns_environment),
        ("taskId", task_id),
        ("pathway", pathway),
    ):
        if value is not None:
            item[key] = value

    try:
        _table().put_item(Item=item)
    except Exception:  # noqa: BLE001 - logging must never break delivery
        import logging
        logging.getLogger().exception("Failed to write notification log row")
