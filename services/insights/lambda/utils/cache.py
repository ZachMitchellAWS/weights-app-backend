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


def get_cached_starter(user_id: str) -> dict | None:
    """
    Read cached starter insight for a user.

    Uses insightWeek="starter" as the sort key.
    """
    table = _get_cache_table()
    response = table.get_item(Key={'userId': user_id, 'insightWeek': 'starter'})
    return response.get('Item')


def put_cached_starter(
    user_id: str,
    body: str,
    model_version: str,
) -> None:
    """
    Write generated starter insight to cache.

    Uses insightWeek="starter" as the sort key.
    """
    table = _get_cache_table()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    ttl = int(time.time()) + (60 * 60 * 24 * 365)  # 1 year

    table.put_item(
        Item={
            'userId': user_id,
            'insightWeek': 'starter',
            'body': body,
            'generatedAt': now_utc,
            'modelVersion': model_version,
            'ttl': ttl,
        }
    )
    logger.info(f"Cached starter insight for user {user_id}")


def update_starter_audio_key(
    user_id: str,
    audio_key: str,
) -> None:
    """
    Add audio key to the starter cache item.

    Uses UpdateItem so it doesn't overwrite the full item.
    """
    table = _get_cache_table()
    table.update_item(
        Key={'userId': user_id, 'insightWeek': 'starter'},
        UpdateExpression='SET audioKey = :ak',
        ExpressionAttributeValues={':ak': audio_key},
    )
    logger.info(f"Updated starter audio key for user {user_id}")


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


# ---------------------------------------------------------------------------
# Tier Unlock Cache
# ---------------------------------------------------------------------------

_TIER_SK_PREFIX = 'tier-'


def get_cached_tier_unlock(user_id: str, tier_name: str) -> dict | None:
    """Read cached tier unlock insight for a user and tier."""
    table = _get_cache_table()
    response = table.get_item(
        Key={'userId': user_id, 'insightWeek': f'{_TIER_SK_PREFIX}{tier_name.lower()}'}
    )
    return response.get('Item')


def put_cached_tier_unlock(
    user_id: str,
    tier_name: str,
    body: str,
    model_version: str,
) -> None:
    """Write generated tier unlock insight to cache."""
    table = _get_cache_table()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    table.put_item(
        Item={
            'userId': user_id,
            'insightWeek': f'{_TIER_SK_PREFIX}{tier_name.lower()}',
            'body': body,
            'generatedAt': now_utc,
            'modelVersion': model_version,
        }
    )
    logger.info(f"Cached tier unlock insight for user {user_id}, tier {tier_name}")


def get_all_tier_unlocks(user_id: str) -> list[dict]:
    """
    Query all tier unlock insights for a user.

    Runs lazy migration from starter → tier-novice if needed.
    """
    from boto3.dynamodb.conditions import Key

    table = _get_cache_table()
    response = table.query(
        KeyConditionExpression=(
            Key('userId').eq(user_id) &
            Key('insightWeek').begins_with(_TIER_SK_PREFIX)
        )
    )
    items = response.get('Items', [])

    # Lazy migration: if no tier-novice but starter exists, migrate it
    has_novice = any(i['insightWeek'] == f'{_TIER_SK_PREFIX}novice' for i in items)
    if not has_novice:
        migrated = migrate_starter_to_tier_unlock(user_id)
        if migrated:
            items.append(migrated)

    return items


def update_tier_unlock_audio_key(user_id: str, tier_name: str, audio_key: str) -> None:
    """Add audio key to a tier unlock cache item."""
    table = _get_cache_table()
    table.update_item(
        Key={'userId': user_id, 'insightWeek': f'{_TIER_SK_PREFIX}{tier_name.lower()}'},
        UpdateExpression='SET audioKey = :ak',
        ExpressionAttributeValues={':ak': audio_key},
    )
    logger.info(f"Updated tier unlock audio key for user {user_id}, tier {tier_name}")


def migrate_starter_to_tier_unlock(user_id: str) -> dict | None:
    """
    Lazy migration: copy insightWeek='starter' to 'tier-novice' and delete the old item.

    Returns the new tier-novice item if migration occurred, None otherwise.
    """
    table = _get_cache_table()
    starter = get_cached_starter(user_id)
    if not starter:
        return None

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    new_item = {
        'userId': user_id,
        'insightWeek': f'{_TIER_SK_PREFIX}novice',
        'body': starter.get('body', ''),
        'generatedAt': starter.get('generatedAt', now_utc),
        'modelVersion': starter.get('modelVersion', 'unknown'),
    }
    # Carry over audio key if present
    if starter.get('audioKey'):
        new_item['audioKey'] = starter['audioKey']

    table.put_item(Item=new_item)
    table.delete_item(Key={'userId': user_id, 'insightWeek': 'starter'})
    logger.info(f"Migrated starter insight to tier-novice for user {user_id}")
    return new_item
