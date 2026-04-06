"""API Gateway response utilities."""

import json
from decimal import Decimal
from typing import Any, Dict, Optional


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types from DynamoDB."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def create_response(
    status_code: int,
    body: Any,
    headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Create a standardized API Gateway response.

    Args:
        status_code: HTTP status code
        body: Response body (will be JSON-encoded)
        headers: Optional additional headers

    Returns:
        API Gateway response dict
    """
    default_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
        "Access-Control-Allow-Methods": "OPTIONS,POST,GET,DELETE",
    }

    if headers:
        default_headers.update(headers)

    return {
        "statusCode": status_code,
        "headers": default_headers,
        "body": json.dumps(body, cls=DecimalEncoder)
    }
