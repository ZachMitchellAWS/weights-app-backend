"""Notification task queue — sharding, ripeness, claiming.

THE SHAPE. Tasks are keyed `shard` (0..SHARD_COUNT-1) / `dueAtTaskId`
(`{dueBinUtc}#{taskId}`). The sort key leads with a fixed-width UTC timestamp, so ripeness is a
plain key condition on the main table and needs no GSI:

    Key('shard').eq(s) & Key('dueAtTaskId').lt(f"{next_bin}#")

`lt(next_bin)` rather than `lte(now_bin)` is deliberate and is what makes overdue work
self-healing: a task whose bin passed while the Lambda was down is still strictly less than the
next bin, so the following sweep picks it up. There is no separate "missed task" path because
there does not need to be one.

WHY SHARD AT ALL. Every task in a bin would otherwise share one DynamoDB partition, which is the
textbook hot-partition write pattern. The shard spreads writes, and doubles as the unit of
parallelism: a worker owns a contiguous shard range and queries each shard in it. DynamoDB
cannot range-query a partition key, so a worker issues one Query per shard it owns — which is
why SHARD_COUNT is 100 and not 1000. At concurrency 1 that is 100 cheap queries; at 1000 it
would be 1000, and the low end of the dial would be unusable at zero volume.
"""

import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# Fixed for the life of the table: it is baked into every row's partition key, so changing it
# strands existing tasks in shards nobody queries. Concurrency is the tunable dial, not this.
SHARD_COUNT = 100

# Scheduling granularity. A task is not due "at 14:07" — it is due in the 14:00 bin. Anything
# needing tighter timing wants the SEND_NOW pathway instead of a task.
BIN_MINUTES = 15

# A claim older than this is assumed to belong to a dead invocation and may be re-taken. Must
# comfortably exceed the Lambda timeout, or a slow-but-alive worker gets its work stolen.
STALE_CLAIM_MINUTES = 30

_dynamodb = None


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    name = os.environ.get("NOTIFICATION_TASKS_TABLE_NAME")
    if not name:
        raise ValueError("NOTIFICATION_TASKS_TABLE_NAME environment variable not set")
    return _dynamodb.Table(name)


# --------------------------------------------------------------------------- #
# Pure helpers — no AWS, unit-testable offline
# --------------------------------------------------------------------------- #
def bin_for(dt: datetime) -> str:
    """Floor a UTC datetime to its BIN_MINUTES boundary, as a sortable ISO string.

    Fixed width matters: these strings are compared lexicographically as part of the sort key,
    so every component must be zero-padded and the suffix constant.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    floored = dt.replace(minute=(dt.minute // BIN_MINUTES) * BIN_MINUTES, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:00Z")


def next_bin_after(dt: datetime) -> str:
    """The bin immediately after the one containing `dt`. The exclusive upper bound of ripeness."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    floored = dt.replace(minute=(dt.minute // BIN_MINUTES) * BIN_MINUTES, second=0, microsecond=0)
    return bin_for(floored + timedelta(minutes=BIN_MINUTES))


def shard_ranges(concurrency: int, shard_count: int = SHARD_COUNT) -> list[tuple[int, int]]:
    """Split 0..shard_count-1 into `concurrency` contiguous inclusive ranges.

    Every shard lands in exactly one range, with no gaps and no overlap, including when the
    division is uneven (100 into 3) — the remainder is spread one-per-range across the leading
    ranges rather than dumped on the last one. A concurrency above shard_count is clamped, since
    empty ranges would just be wasted invocations.
    """
    concurrency = max(1, min(int(concurrency), shard_count))
    base, remainder = divmod(shard_count, concurrency)
    ranges, start = [], 0
    for i in range(concurrency):
        size = base + (1 if i < remainder else 0)
        ranges.append((start, start + size - 1))
        start += size
    return ranges


def pick_shard() -> int:
    """Shard for a new task. Random rather than hashed on userId: hashing would pin a user to
    one shard forever, so a heavy user's tasks could never spread across workers."""
    return random.randrange(SHARD_COUNT)


def build_task(user_id: str, notification_type: str, due_at: datetime,
               ttl_days: int = 30) -> dict[str, Any]:
    """Assemble a task item. Carries a TYPE, never content — copy is resolved at send time from
    the registry in `notification_types.py`, so editing a string changes what already-queued
    tasks will say."""
    task_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    return {
        "shard": pick_shard(),
        "dueAtTaskId": f"{bin_for(due_at)}#{task_id}",
        "taskId": task_id,
        "userId": user_id,
        "notificationType": notification_type,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Orphan backstop only. Nothing should rely on TTL for correctness — a task that keeps
        # failing transiently should be visible in the log, not silently aged out.
        "ttl": int(expires.timestamp()),
    }


# --------------------------------------------------------------------------- #
# DynamoDB
# --------------------------------------------------------------------------- #
def put_task(item: dict[str, Any]) -> None:
    _table().put_item(Item=item)


def ripe_tasks(shard: int, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
    """Tasks in `shard` whose bin is at or before the one containing `now`."""
    resp = _table().query(
        KeyConditionExpression=(
            Key("shard").eq(shard) & Key("dueAtTaskId").lt(f"{next_bin_after(now)}#")
        ),
        Limit=limit,
    )
    return resp.get("Items", [])


def claim_task(shard: int, due_at_task_id: str, now: datetime) -> bool:
    """Take ownership of a task, or return False if someone already holds it.

    Sharding stops two *workers* competing, but a slow run still executing when the next cron
    fires would double-send. The claim is the guard against that. A claim older than
    STALE_CLAIM_MINUTES is re-takeable so a worker killed mid-flight does not strand its tasks.
    """
    stale_before = (now - timedelta(minutes=STALE_CLAIM_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _table().update_item(
            Key={"shard": shard, "dueAtTaskId": due_at_task_id},
            UpdateExpression="SET claimedAt = :now",
            # `shard` is a DynamoDB RESERVED KEYWORD and must be aliased here. It is fine
            # unaliased in `Key={...}` and in `Key("shard").eq(...)` — boto3's condition
            # builder aliases automatically — but a raw ConditionExpression does not, and the
            # whole UpdateItem fails validation. Same trap that `language` and `timezone` set
            # off on user-properties.
            ConditionExpression=(
                "attribute_exists(#shard) AND "
                "(attribute_not_exists(claimedAt) OR claimedAt < :stale)"
            ),
            ExpressionAttributeNames={"#shard": "shard"},
            ExpressionAttributeValues={
                ":now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ":stale": stale_before,
            },
        )
        return True
    except _table().meta.client.exceptions.ConditionalCheckFailedException:
        return False


def release_task(shard: int, due_at_task_id: str) -> None:
    """Drop the claim so the next sweep retries. For transient failures only — a permanent
    outcome should delete the task instead, or it will be retried forever."""
    _table().update_item(
        Key={"shard": shard, "dueAtTaskId": due_at_task_id},
        UpdateExpression="REMOVE claimedAt",
    )


def delete_task(shard: int, due_at_task_id: str, claimed_at: str) -> None:
    """Delete, but only if the claim is still ours.

    Conditioned on `claimedAt` so a stale-recovery sweep that re-took this task cannot have its
    work deleted out from under it by the original slow worker finishing late.
    """
    try:
        _table().delete_item(
            Key={"shard": shard, "dueAtTaskId": due_at_task_id},
            ConditionExpression="claimedAt = :claimed",
            ExpressionAttributeValues={":claimed": claimed_at},
        )
    except _table().meta.client.exceptions.ConditionalCheckFailedException:
        # Someone else owns it now; leaving it alone is correct.
        pass
