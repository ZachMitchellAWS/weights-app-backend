"""Login handler for authentication."""

import json
import os
from typing import Dict, Any
import traceback

# Import from parent directory (Lambda function structure)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.response import create_response


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle user login requests.

    This is a stub implementation that validates input and returns a mock token.
    In production, this would:
    1. Query DynamoDB for the user by email
    2. Verify the password hash
    3. Generate a JWT token
    4. Return the token

    Args:
        event: API Gateway event containing the request
        context: Lambda context object

    Returns:
        API Gateway response with login result
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        email = body.get("email")
        password = body.get("password")

        # Validate required fields
        if not email or not password:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required fields",
                    "message": "Both email and password are required"
                }
            )

        # Get environment variables (for logging/debugging)
        environment = os.environ.get("ENVIRONMENT", "unknown")
        log_level = os.environ.get("LOG_LEVEL", "INFO")

        # Log the login attempt (in production, use proper logging)
        if log_level == "DEBUG":
            print(f"Login attempt for email: {email} in environment: {environment}")

        # STUB: In production, query DynamoDB and verify password
        # For now, return a successful response with a stub token
        return create_response(
            status_code=200,
            body={
                "message": "Login successful (stub)",
                "token": "stub-token-123",
                "user": {
                    "email": email,
                    "name": "Test User"
                }
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
        # Log the full error for debugging
        print(f"Error in login handler: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "An unexpected error occurred"
            }
        )
