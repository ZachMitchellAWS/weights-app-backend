"""API Gateway response utilities."""

import json
from datetime import datetime
from typing import Dict, Any, Optional


def get_current_datetime_iso() -> str:
    """
    Get current UTC datetime in ISO 8601 format with milliseconds.

    Format: YYYY-MM-DDTHH:MM:SS.sss
    Example: 2024-01-17T19:30:45.123

    Returns:
        Current datetime as ISO string with milliseconds
    """
    now = datetime.utcnow()
    # Format with milliseconds (3 decimal places)
    # strftime with %f gives microseconds (6 digits), we want milliseconds (3 digits)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def create_response(
    status_code: int,
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Create a standardized API Gateway response with CORS headers.

    Args:
        status_code: HTTP status code
        body: Response body dictionary (will be JSON-encoded)
        headers: Optional additional headers

    Returns:
        API Gateway response dictionary
    """
    default_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",  # CORS - allows all origins
        "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    }

    # Merge custom headers with defaults
    if headers:
        default_headers.update(headers)

    return {
        "statusCode": status_code,
        "headers": default_headers,
        "body": json.dumps(body),
    }
