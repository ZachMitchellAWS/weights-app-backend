"""
Entitlements service Lambda handler.

Handles entitlement operations:
- GET /entitlements/status: Get current account status (free/premium)
- POST /entitlements: Process Apple transactions and create entitlement grants
- POST /entitlements/apple-notification: Apple Server Notification V2 webhook

Security: Status and process endpoints use userId from JWT token.
Webhook validates user via appAccountToken in the notification.
"""

import json
import os
import boto3
from boto3.dynamodb.conditions import Key
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from utils.response import create_response
from utils.apple_api import (
    get_apple_api_client,
    fetch_transaction_history,
    parse_notification,
    convert_apple_timestamp_to_iso,
    PRODUCT_ENTITLEMENT_MAPPING,
)

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for entitlements service.

    Routes requests based on HTTP method and path:
    - GET /entitlements/status → get_status()
    - POST /entitlements → process_transactions()
    - POST /entitlements/apple-notification → handle_apple_notification()

    Args:
        event: API Gateway Lambda proxy integration event
        context: Lambda context object

    Returns:
        API Gateway response
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')

        print(f"Request: {http_method} {path}")

        # Route to appropriate handler based on method and path
        if http_method == 'GET' and path.endswith('/entitlements/status'):
            # Authenticated endpoint
            user_id = event.get('requestContext', {}).get('authorizer', {}).get('userId')
            if not user_id:
                return create_response(
                    status_code=401,
                    body={"message": "Unauthorized - user ID not found in token"}
                )
            return get_status(event, user_id)

        elif http_method == 'POST' and path.endswith('/entitlements/apple-notification'):
            # Unauthenticated webhook endpoint
            return handle_apple_notification(event)

        elif http_method == 'POST' and path.endswith('/entitlements'):
            # Authenticated endpoint
            user_id = event.get('requestContext', {}).get('authorizer', {}).get('userId')
            if not user_id:
                return create_response(
                    status_code=401,
                    body={"message": "Unauthorized - user ID not found in token"}
                )
            return process_transactions(event, user_id)

        else:
            return create_response(
                status_code=404,
                body={"message": f"Not found: {http_method} {path}"}
            )

    except Exception as e:
        print(f"Error in handler: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def get_status(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get current account status for a user.

    Returns whether the user has an active premium entitlement and when it expires.

    Response:
    {
        "accountStatus": "free" | "premium",
        "expirationUtc": "2025-12-01T09:32:55.000" | null
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with account status
    """
    try:
        table_name = os.environ.get('ENTITLEMENT_GRANTS_TABLE_NAME')
        if not table_name:
            raise ValueError("ENTITLEMENT_GRANTS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get current UTC time
        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]

        # Query using the GSI to get grants sorted by endUtc
        # We want to find any grant where endUtc > now
        response = table.query(
            IndexName='userId-endUtc-index',
            KeyConditionExpression=Key('userId').eq(user_id) & Key('endUtc').gt(now_utc),
            ScanIndexForward=False,  # Most recent first
            Limit=1,
        )

        items = response.get('Items', [])

        if items:
            # User has an active entitlement
            latest_grant = items[0]
            return create_response(
                status_code=200,
                body={
                    "accountStatus": "premium",
                    "expirationUtc": latest_grant.get('endUtc')
                }
            )
        else:
            # No active entitlement
            return create_response(
                status_code=200,
                body={
                    "accountStatus": "free",
                    "expirationUtc": None
                }
            )

    except Exception as e:
        print(f"Error getting status: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def process_transactions(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Process Apple transactions and create entitlement grants.

    Fetches transaction history from Apple using the provided originalTransactionIds
    and creates entitlement grants for each valid subscription.

    Request body:
    {
        "apple": {
            "originalTransactionIds": ["1000000123456789"]
        }
    }

    Response:
    {
        "activeEntitlements": [...],
        "created": 2,
        "skipped": 1
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with processing results
    """
    try:
        body = json.loads(event.get('body', '{}'))

        # Extract transaction IDs if provided
        original_transaction_ids = []
        apple_data = body.get('apple', {})
        if apple_data:
            ids = apple_data.get('originalTransactionIds', [])
            if isinstance(ids, list):
                original_transaction_ids = ids

        # Process any provided transaction IDs
        if original_transaction_ids:
            client = get_apple_api_client()

            for original_transaction_id in original_transaction_ids:
                try:
                    transactions = fetch_transaction_history(client, original_transaction_id)

                    for transaction in transactions:
                        _create_entitlement_grant(user_id, transaction)

                except Exception as e:
                    print(f"Error processing transaction {original_transaction_id}: {str(e)}")

        # Always return current active entitlements
        active_entitlements = _get_active_entitlements(user_id)

        return create_response(
            status_code=200,
            body={
                "activeEntitlements": active_entitlements,
            }
        )

    except json.JSONDecodeError:
        return create_response(
            status_code=400,
            body={
                "error": "Invalid JSON",
                "message": "Request body must be valid JSON"
            }
        )
    except Exception as e:
        print(f"Error processing transactions: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def handle_apple_notification(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Apple Server Notification V2 webhook.

    Apple sends notifications for subscription events (renewals, cancellations, etc.).
    We extract the userId from appAccountToken, verify the user exists, and create
    entitlement grants based on the transaction history.

    Always returns 200 OK to Apple (even on errors) to prevent retries for
    invalid/unknown users.

    Args:
        event: API Gateway event containing the signed notification

    Returns:
        API Gateway response (always 200)
    """
    try:
        body = event.get('body', '')

        # Parse the notification to get user ID and transaction info
        notification_data = parse_notification(body)

        if not notification_data:
            print("Failed to parse notification")
            return create_response(status_code=200, body={"message": "ok"})

        # Log subscription event before any user validation — captures events
        # even for unknown users. Fire-and-forget: errors never block the response.
        _log_subscription_event(notification_data)

        user_id = notification_data.get('userId')
        original_transaction_id = notification_data.get('originalTransactionId')

        if not user_id:
            print("No appAccountToken (userId) in notification")
            return create_response(status_code=200, body={"message": "ok"})

        # Verify user exists in users table
        users_table_name = os.environ.get('USERS_TABLE_NAME')
        if not users_table_name:
            print("USERS_TABLE_NAME environment variable not set")
            return create_response(status_code=200, body={"message": "ok"})

        users_table = dynamodb.Table(users_table_name)
        user_response = users_table.get_item(Key={'userId': user_id})

        if 'Item' not in user_response:
            print(f"User not found: {user_id}")
            return create_response(status_code=200, body={"message": "ok"})

        print(f"Processing notification for user: {user_id}")

        # Fetch transaction history and create grants
        if original_transaction_id:
            try:
                client = get_apple_api_client()
                transactions = fetch_transaction_history(client, original_transaction_id)

                for transaction in transactions:
                    _create_entitlement_grant(user_id, transaction)

            except Exception as e:
                print(f"Error fetching transaction history: {str(e)}")
                # Still return 200 to Apple

        return create_response(status_code=200, body={"message": "ok"})

    except Exception as e:
        print(f"Error handling Apple notification: {str(e)}")
        import traceback
        traceback.print_exc()
        # Always return 200 to Apple
        return create_response(status_code=200, body={"message": "ok"})


def _create_entitlement_grant(user_id: str, transaction: Dict[str, Any]) -> tuple:
    """
    Create an entitlement grant from a transaction.

    Uses conditional write to only create if userId + startUtc doesn't exist.

    Args:
        user_id: User's unique identifier
        transaction: Transaction data from Apple API

    Returns:
        Tuple of (grant dict or None, was_created bool)
    """
    try:
        table_name = os.environ.get('ENTITLEMENT_GRANTS_TABLE_NAME')
        if not table_name:
            raise ValueError("ENTITLEMENT_GRANTS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Extract transaction data
        product_id = transaction.get('productId', '')
        original_transaction_id = transaction.get('originalTransactionId', '')

        # Get entitlement name from product ID mapping
        entitlement_name = PRODUCT_ENTITLEMENT_MAPPING.get(product_id, 'premium')

        # Convert Apple timestamps (milliseconds) to ISO 8601
        purchase_date_ms = transaction.get('purchaseDate', 0)
        expires_date_ms = transaction.get('expiresDate', 0)

        start_utc = convert_apple_timestamp_to_iso(purchase_date_ms)
        end_utc = convert_apple_timestamp_to_iso(expires_date_ms)

        if not start_utc or not end_utc:
            print(f"Invalid dates in transaction: purchaseDate={purchase_date_ms}, expiresDate={expires_date_ms}")
            return None, False

        # Get current datetime
        current_datetime = datetime.now(timezone.utc).isoformat()

        # Create grant item
        grant_item = {
            'userId': user_id,
            'startUtc': start_utc,
            'endUtc': end_utc,
            'entitlementName': entitlement_name,
            'paymentPlatformSource': 'apple',
            'originalTransactionId': original_transaction_id,
            'productId': product_id,
            'createdDatetime': current_datetime,
            'lastModifiedDatetime': current_datetime,
        }

        # Conditional write - only create if doesn't exist
        try:
            table.put_item(
                Item=grant_item,
                ConditionExpression='attribute_not_exists(userId) AND attribute_not_exists(startUtc)'
            )
            print(f"Created entitlement grant for user {user_id}: {product_id} from {start_utc} to {end_utc}")
            return grant_item, True

        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            # Grant already exists
            print(f"Entitlement grant already exists for user {user_id} at {start_utc}")
            return grant_item, False

    except Exception as e:
        print(f"Error creating entitlement grant: {str(e)}")
        import traceback
        traceback.print_exc()
        return None, False


def _get_active_entitlements(user_id: str) -> List[Dict[str, Any]]:
    """
    Get all active entitlements for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of active entitlement grants
    """
    try:
        table_name = os.environ.get('ENTITLEMENT_GRANTS_TABLE_NAME')
        if not table_name:
            return []

        table = dynamodb.Table(table_name)

        # Get current UTC time
        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]

        # Query using the GSI to get grants where endUtc > now
        response = table.query(
            IndexName='userId-endUtc-index',
            KeyConditionExpression=Key('userId').eq(user_id) & Key('endUtc').gt(now_utc),
            ScanIndexForward=False,
        )

        # Return only client-relevant fields
        return [
            {
                'userId': item['userId'],
                'startUtc': item['startUtc'],
                'endUtc': item['endUtc'],
                'entitlementName': item.get('entitlementName', ''),
            }
            for item in response.get('Items', [])
        ]

    except Exception as e:
        print(f"Error getting active entitlements: {str(e)}")
        return []


def _log_subscription_event(notification_data: Dict[str, Any]) -> None:
    """
    Log a subscription event to the subscription-events table.

    Fire-and-forget: errors are logged but never propagated.
    Called before user validation so events are captured even for unknown users.

    Args:
        notification_data: Parsed notification from parse_notification()
    """
    try:
        table_name = os.environ.get('SUBSCRIPTION_EVENTS_TABLE_NAME')
        if not table_name:
            print("SUBSCRIPTION_EVENTS_TABLE_NAME environment variable not set")
            return

        table = dynamodb.Table(table_name)

        user_id = notification_data.get('userId', '')
        notification_type = notification_data.get('notificationType', '')
        subtype = notification_data.get('subtype', '')
        original_transaction_id = notification_data.get('originalTransactionId', '')
        transaction = notification_data.get('transaction', {})

        now = datetime.now(timezone.utc)
        event_timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.') + f"{now.microsecond // 1000:03d}Z"

        item = {
            'userId': user_id,
            'eventTimestamp': event_timestamp,
            'notificationType': notification_type,
            'originalTransactionId': original_transaction_id,
            'transactionId': transaction.get('transactionId', ''),
            'productId': transaction.get('productId', ''),
            'purchaseDateMs': transaction.get('purchaseDate', 0),
            'expiresDateMs': transaction.get('expiresDate', 0),
        }

        # Only include subtype if present (not all notification types have one)
        if subtype:
            item['subtype'] = subtype

        table.put_item(Item=item)

        subtype_str = f"/{subtype}" if subtype else ""
        print(f"Logged subscription event: {notification_type}{subtype_str} for user {user_id}")

    except Exception as e:
        print(f"Error logging subscription event: {str(e)}")
        import traceback
        traceback.print_exc()
