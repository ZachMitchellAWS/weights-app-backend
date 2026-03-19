"""Insights cache read/write for DynamoDB."""

import os
import logging
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

dynamodb = boto3.resource('dynamodb')


def _get_cache_table():
    table_name = os.environ.get('INSIGHTS_CACHE_TABLE_NAME')
    if not table_name:
        raise ValueError("INSIGHTS_CACHE_TABLE_NAME environment variable not set")
    return dynamodb.Table(table_name)


def get_cached_insights(user_id: str, week_start_date: str) -> dict | None:
    """
    Read cached insights for a user's week.

    Args:
        user_id: The user's unique identifier
        week_start_date: Monday date string "YYYY-MM-DD"

    Returns:
        Cache item dict if found, None otherwise
    """
    table = _get_cache_table()
    response = table.get_item(Key={'userId': user_id, 'insightWeek': week_start_date})
    return response.get('Item')


def put_cached_insights(
    user_id: str,
    week_start_date: str,
    sections: list[dict],
    model_version: str,
) -> None:
    """
    Write generated insights to cache.

    Args:
        user_id: The user's unique identifier
        week_start_date: Monday date string "YYYY-MM-DD"
        sections: List of {title, body} dicts
        model_version: OpenAI model used for generation
    """
    table = _get_cache_table()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    ttl = int(time.time()) + (60 * 60 * 24 * 90)  # 90 days

    table.put_item(
        Item={
            'userId': user_id,
            'insightWeek': week_start_date,
            'sections': sections,
            'generatedAt': now_utc,
            'modelVersion': model_version,
            'ttl': ttl,
        }
    )
    logger.info(f"Cached insights for user {user_id}, week {week_start_date}")


def update_audio_keys(
    user_id: str,
    week_start_date: str,
    audio_keys: list[str],
) -> None:
    """
    Add audio keys to an existing cache item.

    Uses UpdateItem so it doesn't overwrite the full item.
    """
    table = _get_cache_table()
    table.update_item(
        Key={'userId': user_id, 'insightWeek': week_start_date},
        UpdateExpression='SET audioKeys = :ak',
        ExpressionAttributeValues={':ak': audio_keys},
    )
    logger.info(f"Updated audio keys for user {user_id}, week {week_start_date}")
