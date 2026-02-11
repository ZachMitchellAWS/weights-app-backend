"""Apple App Store Server API utilities."""

import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import boto3

# Import Apple App Store Server Library
from appstoreserverlibrary.api_client import AppStoreServerAPIClient, Environment
from appstoreserverlibrary.models.TransactionHistoryRequest import (
    TransactionHistoryRequest,
    ProductType,
    Order,
)
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

# SSM client for fetching credentials
ssm = boto3.client('ssm')

# Cache for SSM parameters (to avoid repeated API calls)
_ssm_cache: Dict[str, str] = {}


# Product ID to entitlement name mapping
# Update this mapping with your actual Apple product IDs
PRODUCT_ENTITLEMENT_MAPPING = {
    "com.app.premium.monthly": "premium",
    "com.app.premium.annual": "premium",
}


def _get_ssm_parameter(param_name: str) -> str:
    """
    Get a parameter from SSM Parameter Store with caching.

    Args:
        param_name: Full parameter name/path

    Returns:
        Parameter value
    """
    if param_name not in _ssm_cache:
        response = ssm.get_parameter(Name=param_name, WithDecryption=True)
        _ssm_cache[param_name] = response['Parameter']['Value']
    return _ssm_cache[param_name]


def get_apple_credentials() -> Dict[str, str]:
    """
    Get Apple credentials from SSM Parameter Store.

    Returns:
        Dict with private_key, key_id, issuer_id, bundle_id
    """
    private_key_param = os.environ.get('APPLE_PRIVATE_KEY_PARAM')
    key_id_param = os.environ.get('APPLE_KEY_ID_PARAM')
    issuer_id_param = os.environ.get('APPLE_ISSUER_ID_PARAM')
    bundle_id_param = os.environ.get('APPLE_BUNDLE_ID_PARAM')

    return {
        'private_key': _get_ssm_parameter(private_key_param),
        'key_id': _get_ssm_parameter(key_id_param),
        'issuer_id': _get_ssm_parameter(issuer_id_param),
        'bundle_id': _get_ssm_parameter(bundle_id_param),
    }


def get_apple_environment() -> Environment:
    """
    Get the Apple environment based on deployment environment.

    Returns:
        Environment.SANDBOX for staging, Environment.PRODUCTION for production
    """
    env_str = os.environ.get('APPLE_ENVIRONMENT', 'Sandbox')
    if env_str == 'Production':
        return Environment.PRODUCTION
    return Environment.SANDBOX


def get_apple_api_client() -> AppStoreServerAPIClient:
    """
    Create an Apple App Store Server API client.

    Returns:
        Configured AppStoreServerAPIClient instance
    """
    credentials = get_apple_credentials()
    environment = get_apple_environment()

    return AppStoreServerAPIClient(
        signing_key=credentials['private_key'],
        key_id=credentials['key_id'],
        issuer_id=credentials['issuer_id'],
        bundle_id=credentials['bundle_id'],
        environment=environment,
    )


def fetch_transaction_history(
    client: AppStoreServerAPIClient,
    original_transaction_id: str
) -> List[Dict[str, Any]]:
    """
    Fetch transaction history for an original transaction ID.

    Uses the originalTransactionId to fetch all related transactions
    (renewals, etc.) from Apple.

    Args:
        client: Apple API client
        original_transaction_id: The original transaction ID

    Returns:
        List of transaction dictionaries
    """
    transactions = []

    # Create request for auto-renewable subscriptions
    request = TransactionHistoryRequest(
        sort=Order.DESCENDING,
        productTypes=[ProductType.AUTO_RENEWABLE],
    )

    # Fetch transaction history
    response = client.get_transaction_history(
        transaction_id=original_transaction_id,
        revision=None,
        transaction_history_request=request,
    )

    # Process signed transactions
    if response and response.signed_transactions:
        credentials = get_apple_credentials()
        environment = get_apple_environment()

        # Create verifier for decoding signed transactions
        verifier = SignedDataVerifier(
            root_certificates=[],  # Apple's root certs are included in the library
            enable_online_checks=False,
            environment=environment,
            bundle_id=credentials['bundle_id'],
            app_apple_id=None,  # Not required for subscription verification
        )

        for signed_transaction in response.signed_transactions:
            try:
                # Decode the signed transaction
                transaction = verifier.verify_and_decode_signed_transaction(signed_transaction)
                transactions.append(_transaction_to_dict(transaction))
            except Exception as e:
                print(f"Error decoding transaction: {str(e)}")

    # Handle pagination if there are more results
    while response and response.has_more and response.revision:
        response = client.get_transaction_history(
            transaction_id=original_transaction_id,
            revision=response.revision,
            transaction_history_request=request,
        )

        if response and response.signed_transactions:
            for signed_transaction in response.signed_transactions:
                try:
                    transaction = verifier.verify_and_decode_signed_transaction(signed_transaction)
                    transactions.append(_transaction_to_dict(transaction))
                except Exception as e:
                    print(f"Error decoding transaction: {str(e)}")

    return transactions


def _transaction_to_dict(transaction) -> Dict[str, Any]:
    """
    Convert a transaction object to a dictionary.

    Args:
        transaction: Transaction object from the library

    Returns:
        Dictionary with transaction data
    """
    return {
        'originalTransactionId': getattr(transaction, 'original_transaction_id', ''),
        'transactionId': getattr(transaction, 'transaction_id', ''),
        'productId': getattr(transaction, 'product_id', ''),
        'purchaseDate': getattr(transaction, 'purchase_date', 0),
        'expiresDate': getattr(transaction, 'expires_date', 0),
        'quantity': getattr(transaction, 'quantity', 1),
        'type': str(getattr(transaction, 'type', '')),
        'appAccountToken': getattr(transaction, 'app_account_token', ''),
    }


def parse_notification(body: str) -> Optional[Dict[str, Any]]:
    """
    Parse an Apple Server Notification V2.

    Extracts the userId (from appAccountToken) and transaction information
    from the notification payload.

    Args:
        body: The raw notification body (may be JSON or signed payload)

    Returns:
        Dict with userId and transaction info, or None if parsing fails
    """
    try:
        # Try to parse as JSON first
        try:
            notification_json = json.loads(body)
        except json.JSONDecodeError:
            # Body might be the signed payload directly
            notification_json = {'signedPayload': body}

        signed_payload = notification_json.get('signedPayload')
        if not signed_payload:
            print("No signedPayload in notification")
            return None

        # Create verifier to decode the notification
        credentials = get_apple_credentials()
        environment = get_apple_environment()

        verifier = SignedDataVerifier(
            root_certificates=[],
            enable_online_checks=False,
            environment=environment,
            bundle_id=credentials['bundle_id'],
            app_apple_id=None,
        )

        # Verify and decode the notification
        notification = verifier.verify_and_decode_notification(signed_payload)

        if not notification:
            print("Failed to verify notification")
            return None

        # Extract data from the notification
        notification_type = getattr(notification, 'notification_type', '')
        subtype = getattr(notification, 'subtype', '')

        print(f"Notification type: {notification_type}, subtype: {subtype}")

        # Get the transaction data
        data = getattr(notification, 'data', None)
        if not data:
            print("No data in notification")
            return None

        signed_transaction_info = getattr(data, 'signed_transaction_info', None)
        if not signed_transaction_info:
            print("No signedTransactionInfo in notification data")
            return None

        # Decode the transaction info
        transaction = verifier.verify_and_decode_signed_transaction(signed_transaction_info)
        if not transaction:
            print("Failed to decode transaction info")
            return None

        # Extract appAccountToken (this is the userId set by the iOS app)
        app_account_token = getattr(transaction, 'app_account_token', '')
        original_transaction_id = getattr(transaction, 'original_transaction_id', '')

        return {
            'userId': app_account_token,
            'originalTransactionId': original_transaction_id,
            'notificationType': notification_type,
            'subtype': subtype,
            'transaction': _transaction_to_dict(transaction),
        }

    except Exception as e:
        print(f"Error parsing notification: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def convert_apple_timestamp_to_iso(timestamp_ms: int) -> Optional[str]:
    """
    Convert Apple timestamp (milliseconds since epoch) to ISO 8601 string.

    Args:
        timestamp_ms: Timestamp in milliseconds

    Returns:
        ISO 8601 formatted string (e.g., "2025-11-01T09:32:55.000"), or None if invalid
    """
    if not timestamp_ms or timestamp_ms <= 0:
        return None

    try:
        # Convert milliseconds to seconds
        timestamp_s = timestamp_ms / 1000
        dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        # Format with milliseconds but without timezone suffix for consistency
        return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}'
    except (ValueError, OSError) as e:
        print(f"Error converting timestamp {timestamp_ms}: {str(e)}")
        return None
