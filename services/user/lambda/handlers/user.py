"""User handler for user properties endpoints."""

import json
import os
from typing import Dict, Any
import traceback
import boto3

# Import from parent directory (Lambda function structure)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.response import create_response, get_current_datetime_iso


# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle user service requests.

    Routes based on the API Gateway path and method:
    - GET /user/properties: Get user properties for authenticated user
    - POST /user/properties: Update user properties for authenticated user

    Both endpoints require authentication via Lambda Authorizer.
    The user ID is extracted from the authorizer context.

    Args:
        event: API Gateway event containing the request
        context: Lambda context object

    Returns:
        API Gateway response
    """
    try:
        # Get the path and HTTP method from the API Gateway event
        path = event.get("path", "")
        http_method = event.get("httpMethod", "")
        print(f"Handling request: {http_method} {path}")

        # Route to appropriate handler based on path and method
        if path.endswith("/user/properties"):
            if http_method == "GET":
                return handle_get_properties(event)
            elif http_method == "POST":
                return handle_update_properties(event)
            else:
                return create_response(
                    status_code=405,
                    body={
                        "error": "Method not allowed",
                        "message": f"Method {http_method} not supported for {path}"
                    }
                )
        else:
            return create_response(
                status_code=404,
                body={
                    "error": "Not Found",
                    "message": f"Unknown path: {path}"
                }
            )

    except Exception as e:
        # Log the full error for debugging
        print(f"Unexpected error in user handler: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "An unexpected error occurred"
            }
        )


def handle_get_properties(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle GET /user/properties requests.

    Retrieves user properties for the authenticated user.
    The user ID is extracted from the Lambda Authorizer context.

    Response:
        {
            "userId": "user-uuid",
            "placeholderBool": true,
            "createdDatetime": "2024-01-17T19:30:45.123",
            "lastModifiedDatetime": "2024-01-17T19:35:22.456"
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Get user ID from authorizer context
        request_context = event.get("requestContext", {})
        authorizer = request_context.get("authorizer", {})
        user_id = authorizer.get("userId")

        if not user_id:
            return create_response(
                status_code=401,
                body={
                    "error": "Unauthorized",
                    "message": "User ID not found in authorization context"
                }
            )

        # Get table name from environment
        table_name = os.environ.get("USER_PROPERTIES_TABLE_NAME")
        if not table_name:
            raise ValueError("USER_PROPERTIES_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get user properties from DynamoDB
        response = table.get_item(Key={"userId": user_id})
        user_properties = response.get("Item")

        if not user_properties:
            return create_response(
                status_code=404,
                body={
                    "error": "Not found",
                    "message": "User properties not found"
                }
            )

        print(f"Retrieved properties for user: {user_id}")

        # Return user properties
        return create_response(
            status_code=200,
            body=user_properties
        )

    except Exception as e:
        print(f"Error in handle_get_properties: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to retrieve user properties"
            }
        )


def handle_update_properties(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /user/properties requests.

    Updates user properties for the authenticated user.
    The user ID is extracted from the Lambda Authorizer context.

    Request body:
        {
            "placeholderBool": true
        }

    Response:
        {
            "userId": "user-uuid",
            "placeholderBool": true,
            "createdDatetime": "2024-01-17T19:30:45.123",
            "lastModifiedDatetime": "2024-01-17T19:35:22.456"
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Get user ID from authorizer context
        request_context = event.get("requestContext", {})
        authorizer = request_context.get("authorizer", {})
        user_id = authorizer.get("userId")

        if not user_id:
            return create_response(
                status_code=401,
                body={
                    "error": "Unauthorized",
                    "message": "User ID not found in authorization context"
                }
            )

        # Parse request body
        body = json.loads(event.get("body", "{}"))
        placeholder_bool = body.get("placeholderBool")

        # Validate placeholderBool is provided and is boolean
        if placeholder_bool is None:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "placeholderBool is required"
                }
            )

        if not isinstance(placeholder_bool, bool):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid field type",
                    "message": "placeholderBool must be a boolean"
                }
            )

        # Get table name from environment
        table_name = os.environ.get("USER_PROPERTIES_TABLE_NAME")
        if not table_name:
            raise ValueError("USER_PROPERTIES_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get current datetime for lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Update user properties in DynamoDB
        response = table.update_item(
            Key={"userId": user_id},
            UpdateExpression="SET placeholderBool = :bool, lastModifiedDatetime = :datetime",
            ExpressionAttributeValues={
                ":bool": placeholder_bool,
                ":datetime": current_datetime
            },
            ReturnValues="ALL_NEW"
        )

        updated_properties = response.get("Attributes")

        print(f"Updated properties for user: {user_id}")

        # Return updated user properties
        return create_response(
            status_code=200,
            body=updated_properties
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
        print(f"Error in handle_update_properties: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to update user properties"
            }
        )
