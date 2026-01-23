"""Register handler for user registration."""

import json
import os
from typing import Dict, Any
from datetime import datetime
import traceback

# Import from parent directory (Lambda function structure)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.response import create_response


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle user registration requests.

    This is a stub implementation that validates input and returns success.
    In production, this would:
    1. Validate email doesn't already exist in DynamoDB
    2. Hash the password
    3. Create user record in DynamoDB
    4. Return success with user details

    Args:
        event: API Gateway event containing the request
        context: Lambda context object

    Returns:
        API Gateway response with registration result
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        email = body.get("email")
        password = body.get("password")
        name = body.get("name")

        # Validate required fields
        if not email or not password or not name:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required fields",
                    "message": "email, password, and name are all required"
                }
            )

        # Get environment variables
        table_name = os.environ.get("USERS_TABLE_NAME", "")
        environment = os.environ.get("ENVIRONMENT", "unknown")
        log_level = os.environ.get("LOG_LEVEL", "INFO")

        # Log the registration attempt
        if log_level == "DEBUG":
            print(f"Registration attempt for email: {email}")
            print(f"Using table: {table_name} in environment: {environment}")

        # STUB: In production, this would:
        # 1. Check if email already exists
        # 2. Hash the password
        # 3. Write to DynamoDB

        # Create stub user object
        user_data = {
            "email": email,
            "name": name,
            "created_at": datetime.utcnow().isoformat(),
        }

        return create_response(
            status_code=201,
            body={
                "message": "User registered successfully (stub)",
                "user": user_data
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
        print(f"Error in register handler: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "An unexpected error occurred"
            }
        )
