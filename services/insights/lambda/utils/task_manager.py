"""Insight task management for scheduling and processing insight generation."""

import os
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger(__name__)

dynamodb = boto3.resource('dynamodb')

STALE_THRESHOLD_SECONDS = 300  # 5 minutes


def _get_tasks_table():
    table_name = os.environ.get('INSIGHT_TASKS_TABLE_NAME')
    if not table_name:
        raise ValueError("INSIGHT_TASKS_TABLE_NAME environment variable not set")
    return dynamodb.Table(table_name)


def get_insight_week(dt_iso: str, tz_str: str) -> tuple[str, str]:
    """
    Determine the Monday-Sunday insight week containing a given datetime.

    Args:
        dt_iso: ISO 8601 datetime string (e.g. "2026-03-05T10:30:00.000Z")
        tz_str: IANA timezone string (e.g. "America/Los_Angeles")

    Returns:
        Tuple of (week_start_date, eligible_after_utc) where:
        - week_start_date: Monday date string "YYYY-MM-DD"
        - eligible_after_utc: ISO 8601 UTC string for Sunday 23:59:59 of that week
    """
    user_tz = ZoneInfo(tz_str)
    # Parse the ISO datetime and convert to user's timezone
    dt = datetime.fromisoformat(dt_iso.replace('Z', '+00:00'))
    local_dt = dt.astimezone(user_tz)
    local_date = local_dt.date()

    # Monday of that week (weekday() returns 0=Monday)
    monday = local_date - timedelta(days=local_date.weekday())
    sunday = monday + timedelta(days=6)

    # Sunday 23:59:59 in user's timezone, converted to UTC
    sunday_end_local = datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59, tzinfo=user_tz)
    eligible_after_utc = sunday_end_local.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    week_start_date = monday.isoformat()
    return week_start_date, eligible_after_utc


def schedule_task(user_id: str, timezone_str: str, created_datetime: str) -> bool:
    """
    Conditionally create an insight task for a user's week.

    Only creates if no task already exists for this user+week. Uses a conditional
    put to prevent overwriting existing tasks.

    Args:
        user_id: The user's unique identifier
        timezone_str: IANA timezone from the lift set
        created_datetime: ISO 8601 datetime of the lift set

    Returns:
        True if task was created, False if it already existed
    """
    table = _get_tasks_table()
    week_start, eligible_after_utc = get_insight_week(created_datetime, timezone_str)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    ttl = int(time.time()) + (60 * 60 * 24 * 60)  # 60 days

    try:
        table.put_item(
            Item={
                'userId': user_id,
                'insightWeek': week_start,
                'taskStatus': 'pending',
                'eligibleAfterUtc': eligible_after_utc,
                'processingStartedAt': None,
                'createdAt': now_utc,
                'ttl': ttl,
            },
            ConditionExpression='attribute_not_exists(userId) AND attribute_not_exists(insightWeek)',
        )
        logger.info(f"Created insight task for user {user_id}, week {week_start}")
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(f"Insight task already exists for user {user_id}, week {week_start}")
        return False


def get_ripe_tasks(limit: int = 10) -> list[dict]:
    """
    Query for pending tasks whose eligibleAfterUtc has passed, plus stale processing tasks.

    Args:
        limit: Maximum number of tasks to return

    Returns:
        List of task items ready for processing
    """
    table = _get_tasks_table()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Get pending tasks that are eligible
    pending_response = table.query(
        IndexName='taskStatus-eligibleAfterUtc-index',
        KeyConditionExpression=(
            Key('taskStatus').eq('pending') &
            Key('eligibleAfterUtc').lte(now_utc)
        ),
        Limit=limit,
    )
    tasks = pending_response.get('Items', [])

    # Also get stale processing tasks (processingStartedAt > 5 min ago)
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_THRESHOLD_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    processing_response = table.query(
        IndexName='taskStatus-eligibleAfterUtc-index',
        KeyConditionExpression=(
            Key('taskStatus').eq('processing') &
            Key('eligibleAfterUtc').lte(now_utc)
        ),
        Limit=limit,
    )
    for item in processing_response.get('Items', []):
        started_at = item.get('processingStartedAt')
        if started_at and started_at < stale_cutoff:
            tasks.append(item)

    return tasks[:limit]


def claim_task(user_id: str, insight_week: str) -> bool:
    """
    Atomically claim a task for processing.

    Transitions pending → processing, or re-claims a stale processing task.
    Uses a conditional update to prevent race conditions.

    Args:
        user_id: The user's unique identifier
        insight_week: The Monday date string for the insight week

    Returns:
        True if task was claimed, False if another processor got it first
    """
    table = _get_tasks_table()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_THRESHOLD_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')

    try:
        table.update_item(
            Key={'userId': user_id, 'insightWeek': insight_week},
            UpdateExpression='SET taskStatus = :processing, processingStartedAt = :now',
            ConditionExpression=(
                '(taskStatus = :pending) OR '
                '(taskStatus = :processing_status AND processingStartedAt < :stale_cutoff)'
            ),
            ExpressionAttributeValues={
                ':processing': 'processing',
                ':pending': 'pending',
                ':processing_status': 'processing',
                ':now': now_utc,
                ':stale_cutoff': stale_cutoff,
            },
        )
        logger.info(f"Claimed task for user {user_id}, week {insight_week}")
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(f"Failed to claim task for user {user_id}, week {insight_week} — another processor got it")
        return False


def delete_task(user_id: str, insight_week: str) -> None:
    """Delete a completed task."""
    table = _get_tasks_table()
    table.delete_item(Key={'userId': user_id, 'insightWeek': insight_week})
    logger.info(f"Deleted task for user {user_id}, week {insight_week}")


def get_task(user_id: str, insight_week: str) -> dict | None:
    """Get a specific task by user and week."""
    table = _get_tasks_table()
    response = table.get_item(Key={'userId': user_id, 'insightWeek': insight_week})
    return response.get('Item')


def create_processing_task(user_id: str, insight_week: str, eligible_after_utc: str) -> None:
    """Create a task directly in processing state (for ad-hoc generation)."""
    table = _get_tasks_table()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    ttl = int(time.time()) + (60 * 60 * 24 * 60)  # 60 days

    table.put_item(
        Item={
            'userId': user_id,
            'insightWeek': insight_week,
            'taskStatus': 'processing',
            'eligibleAfterUtc': eligible_after_utc,
            'processingStartedAt': now_utc,
            'createdAt': now_utc,
            'ttl': ttl,
        },
    )
    logger.info(f"Created processing task for user {user_id}, week {insight_week}")
