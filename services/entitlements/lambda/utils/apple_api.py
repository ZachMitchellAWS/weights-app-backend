"""Apple App Store Server API utilities."""

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import boto3

# Load Apple Root CA certificate for transaction verification
_CERTS_DIR = Path(__file__).parent.parent / "certs"
_APPLE_ROOT_CA = (_CERTS_DIR / "AppleRootCA-G3.cer").read_bytes()

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


def _get_app_apple_id() -> Optional[int]:
    """
    Get the App Store app ID from environment.

    Required by SignedDataVerifier when verifying Production transactions.
    Not required for Sandbox verification.

    Returns:
        The app's numeric Apple ID, or None if unset.
    """
    value = os.environ.get('APPLE_APP_APPLE_ID')
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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


def resolve_environment_override(requested_env: Optional[str]) -> Optional[Environment]:
    """
    Resolve a client-requested environment override.

    Only allows sandbox override when the deployment environment is Production.
    This enables TestFlight builds (which use Apple Sandbox) to validate
    transactions against the production backend.

    Args:
        requested_env: Environment string from request body ("Sandbox" or None)

    Returns:
        Environment.SANDBOX if override is valid, None otherwise
    """
    if requested_env == "Sandbox" and get_apple_environment() == Environment.PRODUCTION:
        return Environment.SANDBOX
    return None


def get_apple_api_client(
    environment_override: Optional[Environment] = None,
) -> AppStoreServerAPIClient:
    """
    Create an Apple App Store Server API client.

    Args:
        environment_override: Optional environment override (e.g. SANDBOX for
            TestFlight transactions on the production backend)

    Returns:
        Configured AppStoreServerAPIClient instance
    """
    credentials = get_apple_credentials()
    environment = get_apple_environment()

    # Apply override only when running in production
    if environment_override and environment == Environment.PRODUCTION:
        environment = environment_override

    return AppStoreServerAPIClient(
        signing_key=credentials['private_key'].encode('utf-8'),
        key_id=credentials['key_id'],
        issuer_id=credentials['issuer_id'],
        bundle_id=credentials['bundle_id'],
        environment=environment,
    )


def fetch_transaction_history(
    client: AppStoreServerAPIClient,
    original_transaction_id: str,
    environment_override: Optional[Environment] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch transaction history for an original transaction ID.

    Uses the originalTransactionId to fetch all related transactions
    (renewals, etc.) from Apple.

    Args:
        client: Apple API client
        original_transaction_id: The original transaction ID
        environment_override: Optional environment override for signature
            verification (e.g. SANDBOX for TestFlight on production)

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
    if response and response.signedTransactions:
        credentials = get_apple_credentials()
        environment = get_apple_environment()

        # Apply override only when running in production
        if environment_override and environment == Environment.PRODUCTION:
            environment = environment_override

        # Create verifier for decoding signed transactions. app_apple_id is
        # required by the library when verifying Production transactions.
        app_apple_id = _get_app_apple_id() if environment == Environment.PRODUCTION else None
        verifier = SignedDataVerifier(
            root_certificates=[_APPLE_ROOT_CA],  # Apple's root certs are included in the library
            enable_online_checks=False,
            environment=environment,
            bundle_id=credentials['bundle_id'],
            app_apple_id=app_apple_id,
        )

        for signed_transaction in response.signedTransactions:
            try:
                # Decode the signed transaction
                transaction = verifier.verify_and_decode_signed_transaction(signed_transaction)
                transactions.append(_transaction_to_dict(transaction))
            except Exception as e:
                print(f"Error decoding transaction: {str(e)}")

    # Handle pagination if there are more results
    while response and response.hasMore and response.revision:
        response = client.get_transaction_history(
            transaction_id=original_transaction_id,
            revision=response.revision,
            transaction_history_request=request,
        )

        if response and response.signedTransactions:
            for signed_transaction in response.signedTransactions:
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
        'originalTransactionId': getattr(transaction, 'originalTransactionId', ''),
        'transactionId': getattr(transaction, 'transactionId', ''),
        'productId': getattr(transaction, 'productId', ''),
        'purchaseDate': getattr(transaction, 'purchaseDate', 0),
        'expiresDate': getattr(transaction, 'expiresDate', 0),
        'quantity': getattr(transaction, 'quantity', 1),
        'type': str(getattr(transaction, 'type', '')),
        'appAccountToken': getattr(transaction, 'appAccountToken', ''),
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

        app_apple_id = _get_app_apple_id() if environment == Environment.PRODUCTION else None
        verifier = SignedDataVerifier(
            root_certificates=[_APPLE_ROOT_CA],
            enable_online_checks=False,
            environment=environment,
            bundle_id=credentials['bundle_id'],
            app_apple_id=app_apple_id,
        )

        # Verify and decode the notification
        notification = verifier.verify_and_decode_notification(signed_payload)
        transaction_environment = "Production" if environment == Environment.PRODUCTION else "Sandbox"

        if not notification:
            print("Failed to verify notification")
            return None

        # Extract data from the notification
        notification_type = getattr(notification, 'rawNotificationType', '') or ''
        subtype = getattr(notification, 'rawSubtype', '') or ''

        print(f"Notification type: {notification_type}, subtype: {subtype}")

        # Get the transaction data
        data = getattr(notification, 'data', None)
        if not data:
            print("No data in notification")
            return None

        signed_transaction_info = getattr(data, 'signedTransactionInfo', None)
        if not signed_transaction_info:
            print("No signedTransactionInfo in notification data")
            return None

        # Decode the transaction info
        transaction = verifier.verify_and_decode_signed_transaction(signed_transaction_info)
        if not transaction:
            print("Failed to decode transaction info")
            return None

        # Extract appAccountToken (this is the userId set by the iOS app)
        app_account_token = getattr(transaction, 'appAccountToken', '')
        original_transaction_id = getattr(transaction, 'originalTransactionId', '')

        return {
            'userId': app_account_token,
            'originalTransactionId': original_transaction_id,
            'notificationType': notification_type,
            'subtype': subtype,
            'transaction': _transaction_to_dict(transaction),
            'transactionEnvironment': transaction_environment,
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
