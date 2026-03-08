"""User handler for user properties endpoints."""

import json
import os
from decimal import Decimal
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
            "availableChangePlates": [2.5, 5, 10, 25, 35, 45],
            "bodyweight": 185.5,
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
    Only provided fields are updated (partial update supported).

    Request body (all fields optional, at least one required):
        {
            "bodyweight": 185.5,
            "availableChangePlates": [2.5, 5, 10, 25, 35, 45]
        }

    Response:
        {
            "userId": "user-uuid",
            "bodyweight": 185.5,
            "availableChangePlates": [2.5, 5, 10, 25, 35, 45],
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

        # Build update expression dynamically based on provided fields
        update_parts = []
        remove_parts = []
        expression_values = {}

        # Handle bodyweight (nullable - can be set or removed)
        if "bodyweight" in body:
            bodyweight = body.get("bodyweight")
            if bodyweight is None:
                # Remove bodyweight if explicitly set to null
                remove_parts.append("bodyweight")
            elif isinstance(bodyweight, (int, float)):
                update_parts.append("bodyweight = :bodyweight")
                # Convert to Decimal for DynamoDB
                expression_values[":bodyweight"] = Decimal(str(bodyweight))
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "bodyweight must be a number or null"
                    }
                )

        # Handle availableChangePlates
        if "availableChangePlates" in body:
            available_change_plates = body.get("availableChangePlates")
            if not isinstance(available_change_plates, list):
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "availableChangePlates must be a list"
                    }
                )
            # Validate all items are numbers
            for item in available_change_plates:
                if not isinstance(item, (int, float)):
                    return create_response(
                        status_code=400,
                        body={
                            "error": "Invalid field type",
                            "message": "availableChangePlates must contain only numbers"
                        }
                    )
            update_parts.append("availableChangePlates = :availableChangePlates")
            # Convert floats to Decimal for DynamoDB
            expression_values[":availableChangePlates"] = [
                Decimal(str(item)) for item in available_change_plates
            ]

        # Handle minReps
        if "minReps" in body:
            min_reps = body.get("minReps")
            if not isinstance(min_reps, int) or min_reps < 1:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "minReps must be a positive integer"
                    }
                )
            update_parts.append("minReps = :minReps")
            expression_values[":minReps"] = min_reps

        # Handle maxReps
        if "maxReps" in body:
            max_reps = body.get("maxReps")
            if not isinstance(max_reps, int) or max_reps < 1:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "maxReps must be a positive integer"
                    }
                )
            update_parts.append("maxReps = :maxReps")
            expression_values[":maxReps"] = max_reps

        # Handle activeSetPlanTemplateId (nullable string - can be set or removed)
        if "activeSetPlanTemplateId" in body:
            active_set_plan = body.get("activeSetPlanTemplateId")
            if active_set_plan is None:
                remove_parts.append("activeSetPlanTemplateId")
            elif isinstance(active_set_plan, str):
                update_parts.append("activeSetPlanTemplateId = :activeSetPlanTemplateId")
                expression_values[":activeSetPlanTemplateId"] = active_set_plan
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "activeSetPlanTemplateId must be a string or null"
                    }
                )

        # Handle activeSplitId (nullable string - can be set or removed)
        if "activeSplitId" in body:
            active_split = body.get("activeSplitId")
            if active_split is None:
                remove_parts.append("activeSplitId")
            elif isinstance(active_split, str):
                update_parts.append("activeSplitId = :activeSplitId")
                expression_values[":activeSplitId"] = active_split
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "activeSplitId must be a string or null"
                    }
                )

        # Handle stepsGoal (nullable integer - can be set or removed)
        if "stepsGoal" in body:
            steps_goal = body.get("stepsGoal")
            if steps_goal is None:
                remove_parts.append("stepsGoal")
            elif isinstance(steps_goal, int) and steps_goal > 0:
                update_parts.append("stepsGoal = :stepsGoal")
                expression_values[":stepsGoal"] = steps_goal
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "stepsGoal must be a positive integer or null"
                    }
                )

        # Handle proteinGoal (nullable integer - can be set or removed)
        if "proteinGoal" in body:
            protein_goal = body.get("proteinGoal")
            if protein_goal is None:
                remove_parts.append("proteinGoal")
            elif isinstance(protein_goal, int) and protein_goal > 0:
                update_parts.append("proteinGoal = :proteinGoal")
                expression_values[":proteinGoal"] = protein_goal
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "proteinGoal must be a positive integer or null"
                    }
                )

        # Handle bodyweightTarget (nullable number - can be set or removed)
        if "bodyweightTarget" in body:
            bw_target = body.get("bodyweightTarget")
            if bw_target is None:
                remove_parts.append("bodyweightTarget")
            elif isinstance(bw_target, (int, float)) and bw_target > 0:
                update_parts.append("bodyweightTarget = :bodyweightTarget")
                expression_values[":bodyweightTarget"] = Decimal(str(bw_target))
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "bodyweightTarget must be a positive number or null"
                    }
                )

        # Handle per-mode effort rep range fields
        for field in ["easyMinReps", "easyMaxReps", "moderateMinReps", "moderateMaxReps", "hardMinReps", "hardMaxReps"]:
            if field in body:
                val = body.get(field)
                if not isinstance(val, int) or val < 1:
                    return create_response(
                        status_code=400,
                        body={
                            "error": "Invalid field type",
                            "message": f"{field} must be a positive integer"
                        }
                    )
                update_parts.append(f"{field} = :{field}")
                expression_values[f":{field}"] = val

        # Require at least one field to update
        if not update_parts and not remove_parts:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing fields",
                    "message": "At least one field must be provided to update"
                }
            )

        # Get table name from environment
        table_name = os.environ.get("USER_PROPERTIES_TABLE_NAME")
        if not table_name:
            raise ValueError("USER_PROPERTIES_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get current datetime for lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Add lastModifiedDatetime to update
        update_parts.append("lastModifiedDatetime = :datetime")
        expression_values[":datetime"] = current_datetime

        # Build final update expression
        update_expression = "SET " + ", ".join(update_parts)
        if remove_parts:
            update_expression += " REMOVE " + ", ".join(remove_parts)

        # Update user properties in DynamoDB
        response = table.update_item(
            Key={"userId": user_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
            ReturnValues="ALL_NEW"
        )

        updated_properties = response.get("Attributes")

        print(f"Updated properties for user: {user_id}, update_expression: {update_expression}, values: {expression_values}")

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
