"""Entitlement check utility for insights service."""

import os
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

dynamodb = boto3.resource('dynamodb')


def check_premium(user_id: str) -> bool:
    """
    Check if a user has an active premium entitlement.

    Queries the entitlement-grants table GSI (userId-endUtc-index) for any grant
    whose endUtc is greater than the current UTC time.

    Args:
        user_id: The user's unique identifier

    Returns:
        True if user has an active premium subscription, False otherwise
    """
    table_name = os.environ.get('ENTITLEMENT_GRANTS_TABLE_NAME')
    if not table_name:
        raise ValueError("ENTITLEMENT_GRANTS_TABLE_NAME environment variable not set")

    table = dynamodb.Table(table_name)

    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]

    response = table.query(
        IndexName='userId-endUtc-index',
        KeyConditionExpression=Key('userId').eq(user_id) & Key('endUtc').gt(now_utc),
        ScanIndexForward=False,
        Limit=1,
    )

    return len(response.get('Items', [])) > 0
