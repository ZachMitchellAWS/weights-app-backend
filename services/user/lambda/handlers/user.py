"""User handler for user properties endpoints."""

import json
import os
from decimal import Decimal
from typing import Dict, Any
from zoneinfo import ZoneInfo
import traceback
import boto3

# Import from parent directory (Lambda function structure)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.response import create_response, get_current_datetime_iso
from utils.sentry_init import init_sentry, set_sentry_user
import sentry_sdk

init_sentry()

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

        # Set Sentry user context
        user_id_from_auth = event.get("requestContext", {}).get("authorizer", {}).get("userId")
        if user_id_from_auth:
            set_sentry_user(user_id_from_auth)

        # Route to appropriate handler based on path and method
        if path.endswith("/user/feedback"):
            if http_method == "POST":
                return handle_submit_feedback(event)
            else:
                return create_response(
                    status_code=405,
                    body={
                        "error": "Method not allowed",
                        "message": f"Method {http_method} not supported for {path}"
                    }
                )
        elif path.endswith("/user/delete-account"):
            if http_method == "POST":
                return handle_delete_account_request(event)
            else:
                return create_response(
                    status_code=405,
                    body={
                        "error": "Method not allowed",
                        "message": f"Method {http_method} not supported for {path}"
                    }
                )
        elif path.endswith("/user/properties"):
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
        sentry_sdk.capture_exception(e)
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
        print(f"Parsed body keys: {list(body.keys())}")

        # Build update expression dynamically based on provided fields
        update_parts = []
        remove_parts = []
        expression_values = {}
        expression_names = {}  # For DynamoDB reserved keywords

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

        # Handle activeSetPlanId (nullable string - can be set or removed)
        if "activeSetPlanId" in body:
            active_set_plan = body.get("activeSetPlanId")
            if active_set_plan is None:
                remove_parts.append("activeSetPlanId")
            elif isinstance(active_set_plan, str):
                update_parts.append("activeSetPlanId = :activeSetPlanId")
                expression_values[":activeSetPlanId"] = active_set_plan
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "activeSetPlanId must be a string or null"
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

        # Handle timezone (nullable string - can be set or removed)
        if "timezone" in body:
            tz_val = body.get("timezone")
            if tz_val is None:
                expression_names["#tz"] = "timezone"
                remove_parts.append("#tz")
            elif isinstance(tz_val, str):
                try:
                    ZoneInfo(tz_val)
                except (KeyError, ValueError):
                    return create_response(
                        status_code=400,
                        body={
                            "error": "Invalid field value",
                            "message": f"timezone must be a valid IANA timezone identifier, got '{tz_val}'"
                        }
                    )
                expression_names["#tz"] = "timezone"
                update_parts.append("#tz = :timezone")
                expression_values[":timezone"] = tz_val
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "timezone must be a string or null"
                    }
                )

        # Handle biologicalSex (nullable string - "male" or "female")
        if "biologicalSex" in body:
            bio_sex = body.get("biologicalSex")
            if bio_sex is None:
                remove_parts.append("biologicalSex")
            elif isinstance(bio_sex, str) and bio_sex in ("male", "female"):
                update_parts.append("biologicalSex = :biologicalSex")
                expression_values[":biologicalSex"] = bio_sex
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field value",
                        "message": "biologicalSex must be 'male', 'female', or null"
                    }
                )

        # Handle weightUnit (non-nullable string - "lbs" or "kg")
        if "weightUnit" in body:
            weight_unit = body.get("weightUnit")
            if isinstance(weight_unit, str) and weight_unit in ("lbs", "kg"):
                update_parts.append("weightUnit = :weightUnit")
                expression_values[":weightUnit"] = weight_unit
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field value",
                        "message": "weightUnit must be 'lbs' or 'kg'"
                    }
                )

        # Handle hasMetStrengthTierConditions (boolean)
        if 'hasMetStrengthTierConditions' in body:
            val = body['hasMetStrengthTierConditions']
            if isinstance(val, bool):
                update_parts.append('hasMetStrengthTierConditions = :hmstc')
                expression_values[':hmstc'] = val
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "hasMetStrengthTierConditions must be a boolean"
                    }
                )

        # Handle apnsDeviceToken (nullable string - can be set or removed)
        if "apnsDeviceToken" in body:
            apns_token = body.get("apnsDeviceToken")
            if apns_token is None:
                remove_parts.append("apnsDeviceToken")
            elif isinstance(apns_token, str) and len(apns_token) <= 200:
                update_parts.append("apnsDeviceToken = :apnsDeviceToken")
                expression_values[":apnsDeviceToken"] = apns_token
            else:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid field type",
                        "message": "apnsDeviceToken must be a string (max 200 chars) or null"
                    }
                )

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
        update_kwargs = {
            "Key": {"userId": user_id},
            "UpdateExpression": update_expression,
            "ExpressionAttributeValues": expression_values,
            "ReturnValues": "ALL_NEW",
        }
        if expression_names:
            update_kwargs["ExpressionAttributeNames"] = expression_names
        response = table.update_item(**update_kwargs)

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


def handle_delete_account_request(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /user/delete-account requests.

    Records an account deletion request for the authenticated user.
    No actual data deletion is performed — requests are stored for manual processing.
    """
    try:
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

        table_name = os.environ.get("DELETION_REQUESTS_TABLE_NAME")
        if not table_name:
            raise ValueError("DELETION_REQUESTS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        table.put_item(Item={
            "userId": user_id,
            "requestedDatetime": get_current_datetime_iso(),
            "status": "pending",
        })

        print(f"Account deletion request recorded for user: {user_id}")

        return create_response(
            status_code=200,
            body={"message": "Account deletion request received"}
        )

    except Exception as e:
        print(f"Error in handle_delete_account_request: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to process account deletion request"
            }
        )


def handle_submit_feedback(event: Dict[str, Any]) -> Dict[str, Any]:
    """Handle POST /user/feedback requests."""
    try:
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

        body = json.loads(event.get("body", "{}"))
        message = body.get("message")

        if not isinstance(message, str) or len(message.strip()) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid field",
                    "message": "message is required and must be a non-empty string"
                }
            )

        if len(message) > 2000:
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid field",
                    "message": "message must be 2000 characters or fewer"
                }
            )

        table_name = os.environ.get("FEEDBACK_TABLE_NAME")
        if not table_name:
            raise ValueError("FEEDBACK_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        table.put_item(Item={
            "userId": user_id,
            "createdDatetime": get_current_datetime_iso(),
            "message": message.strip(),
        })

        print(f"Feedback submitted by user: {user_id}")

        return create_response(
            status_code=200,
            body={"message": "Feedback received"}
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
        print(f"Error in handle_submit_feedback: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to submit feedback"
            }
        )
