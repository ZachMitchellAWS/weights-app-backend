"""
Checkin service Lambda handler.

Handles exercise check-in operations:
- POST /checkin/exercises: Create or update exercises (upsert with batch support)
- GET /checkin/exercises: Get all non-deleted exercises
- DELETE /checkin/exercises: Soft delete exercises (batch support)

Handles lift set operations:
- POST /checkin/lift-sets: Create lift sets (batch support)
- GET /checkin/lift-sets: Get paginated lift sets (most recent first)
- DELETE /checkin/lift-sets: Soft delete lift sets (batch support)

Handles estimated 1RM operations:
- POST /checkin/estimated-1rm: Create estimated 1RM records (batch support)
- GET /checkin/estimated-1rm: Get paginated estimated 1RM records (most recent first)
- DELETE /checkin/estimated-1rm: Soft delete estimated 1RM records (batch support)

Security: All operations use the userId from the JWT token as the DynamoDB
partition key, ensuring users can only access/modify their own data.
"""

import json
import os
import base64
import boto3
from boto3.dynamodb.conditions import Key
from decimal import Decimal
from typing import Dict, Any, List, Optional

from utils.response import create_response
from utils.datetime_utils import get_current_datetime_iso

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for checkin service.

    Routes requests based on HTTP method and path:
    - POST /checkin/exercises → upsert_exercises() (create or update)
    - GET /checkin/exercises → get_exercises()
    - DELETE /checkin/exercises → delete_exercises()

    Args:
        event: API Gateway Lambda proxy integration event
        context: Lambda context object

    Returns:
        API Gateway response
    """
    try:
        # Extract HTTP method and path
        http_method = event.get('httpMethod')
        path = event.get('path', '')

        print(f"Request: {http_method} {path}")

        # Extract user ID from authorizer context
        # The Lambda Authorizer adds this to the request context
        user_id = event.get('requestContext', {}).get('authorizer', {}).get('userId')

        if not user_id:
            return create_response(
                status_code=401,
                body={"message": "Unauthorized - user ID not found in token"}
            )

        # Route to appropriate handler based on method and path
        # Exercise routes
        if http_method == 'POST' and path.endswith('/checkin/exercises'):
            return upsert_exercises(event, user_id)
        elif http_method == 'GET' and path.endswith('/checkin/exercises'):
            return get_exercises(event, user_id)
        elif http_method == 'DELETE' and path.endswith('/checkin/exercises'):
            return delete_exercises(event, user_id)
        # Lift set routes
        elif http_method == 'POST' and path.endswith('/checkin/lift-sets'):
            return create_lift_sets(event, user_id)
        elif http_method == 'GET' and path.endswith('/checkin/lift-sets'):
            return get_lift_sets(event, user_id)
        elif http_method == 'DELETE' and path.endswith('/checkin/lift-sets'):
            return delete_lift_sets(event, user_id)
        # Estimated 1RM routes
        elif http_method == 'POST' and path.endswith('/checkin/estimated-1rm'):
            return create_estimated_1rm(event, user_id)
        elif http_method == 'GET' and path.endswith('/checkin/estimated-1rm'):
            return get_estimated_1rm(event, user_id)
        elif http_method == 'DELETE' and path.endswith('/checkin/estimated-1rm'):
            return delete_estimated_1rm(event, user_id)
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


def upsert_exercises(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Create or update one or more exercise check-ins (upsert with batch support).

    - For new items: creates with all fields including createdDatetime
    - For existing items: updates mutable fields, preserves createdDatetime/createdTimezone

    The userId from the JWT token is used as the partition key, ensuring users
    can only create/update their own exercises.

    Expected request body:
    {
        "exercises": [
            {
                "exerciseItemId": "uuid-string",
                "name": "Exercise name",
                "isCustom": true/false,
                "loadType": "Barbell" | "Single Load",
                "createdTimezone": "America/Los_Angeles",
                "createdDatetime": "2026-01-27T10:30:00.000Z",
                "notes": "Optional notes",
                "icon": "Optional icon identifier"
            },
            ...
        ]
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with created/updated exercises
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate exercises array exists
        if 'exercises' not in body:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "Request body must contain 'exercises' array"
                }
            )

        exercises_input = body['exercises']

        if not isinstance(exercises_input, list):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid format",
                    "message": "'exercises' must be an array"
                }
            )

        if len(exercises_input) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Empty exercises array",
                    "message": "At least one exercise is required"
                }
            )

        # Validate each exercise
        required_fields = ['exerciseItemId', 'name', 'isCustom', 'loadType', 'createdTimezone', 'createdDatetime']
        valid_load_types = ['Barbell', 'Single Load']
        validation_errors = []

        for idx, exercise in enumerate(exercises_input):
            missing_fields = [field for field in required_fields if field not in exercise]
            if missing_fields:
                validation_errors.append(
                    f"Exercise at index {idx}: missing fields: {', '.join(missing_fields)}"
                )
                continue

            if exercise['loadType'] not in valid_load_types:
                validation_errors.append(
                    f"Exercise at index {idx}: loadType must be one of: {', '.join(valid_load_types)}"
                )

            if 'icon' in exercise and exercise['icon'] is not None and not isinstance(exercise['icon'], str):
                validation_errors.append(
                    f"Exercise at index {idx}: icon must be a string or null"
                )

            if 'notes' in exercise and exercise['notes'] is not None and not isinstance(exercise['notes'], str):
                validation_errors.append(
                    f"Exercise at index {idx}: notes must be a string or null"
                )

        if validation_errors:
            return create_response(
                status_code=400,
                body={
                    "error": "Validation failed",
                    "message": "One or more exercises have validation errors",
                    "errors": validation_errors
                }
            )

        # Get table
        table_name = os.environ.get('EXERCISES_TABLE_NAME')
        if not table_name:
            raise ValueError("EXERCISES_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get current datetime
        current_datetime = get_current_datetime_iso()

        # Collect all exerciseItemIds to check which already exist
        exercise_item_ids = [ex['exerciseItemId'] for ex in exercises_input]

        # Batch get existing items to determine create vs update
        # Note: DynamoDB BatchGetItem has a limit of 100 keys per request
        existing_items = {}
        for i in range(0, len(exercise_item_ids), 100):
            batch_ids = exercise_item_ids[i:i + 100]
            keys = [{'userId': user_id, 'exerciseItemId': eid} for eid in batch_ids]

            response = dynamodb.batch_get_item(
                RequestItems={
                    table_name: {
                        'Keys': keys
                    }
                }
            )

            for item in response.get('Responses', {}).get(table_name, []):
                existing_items[item['exerciseItemId']] = item

        # Process each exercise - create or update
        result_exercises = []
        created_count = 0
        updated_count = 0

        for exercise in exercises_input:
            exercise_item_id = exercise['exerciseItemId']
            existing = existing_items.get(exercise_item_id)

            if existing:
                # UPDATE: Item exists for this user - update mutable fields only
                # Preserve: createdDatetime, createdTimezone
                # Update: name, isCustom, loadType, notes, lastModifiedDatetime

                update_expression = 'SET #name = :name, isCustom = :isCustom, loadType = :loadType, lastModifiedDatetime = :lastModified'
                expression_attr_names = {'#name': 'name'}
                expression_attr_values = {
                    ':name': exercise['name'],
                    ':isCustom': exercise['isCustom'],
                    ':loadType': exercise['loadType'],
                    ':lastModified': current_datetime
                }

                # Handle notes - update if provided, remove if explicitly set to None/empty
                if 'notes' in exercise:
                    if exercise['notes']:
                        update_expression += ', notes = :notes'
                        expression_attr_values[':notes'] = exercise['notes']
                    else:
                        # Remove notes if explicitly set to empty/null
                        update_expression += ' REMOVE notes'

                # Handle icon - update if provided, remove if explicitly set to None/empty
                if 'icon' in exercise:
                    if exercise['icon']:
                        update_expression += ', icon = :icon'
                        expression_attr_values[':icon'] = exercise['icon']
                    else:
                        # Remove icon if explicitly set to empty/null
                        update_expression += ' REMOVE icon'

                response = table.update_item(
                    Key={
                        'userId': user_id,
                        'exerciseItemId': exercise_item_id
                    },
                    UpdateExpression=update_expression,
                    ExpressionAttributeNames=expression_attr_names,
                    ExpressionAttributeValues=expression_attr_values,
                    ReturnValues='ALL_NEW'
                )

                result_exercises.append(response.get('Attributes', {}))
                updated_count += 1

            else:
                # CREATE: New item - use frontend-supplied createdDatetime
                exercise_item = {
                    'userId': user_id,
                    'exerciseItemId': exercise_item_id,
                    'name': exercise['name'],
                    'isCustom': exercise['isCustom'],
                    'loadType': exercise['loadType'],
                    'createdTimezone': exercise['createdTimezone'],
                    'createdDatetime': exercise['createdDatetime'],
                    'lastModifiedDatetime': current_datetime,
                }

                # Add optional notes if provided
                if 'notes' in exercise and exercise['notes']:
                    exercise_item['notes'] = exercise['notes']

                # Add optional icon if provided
                if 'icon' in exercise and exercise['icon']:
                    exercise_item['icon'] = exercise['icon']

                table.put_item(Item=exercise_item)
                result_exercises.append(exercise_item)
                created_count += 1

        print(f"Upserted exercises for user {user_id}: {created_count} created, {updated_count} updated")

        # Return result exercises
        return create_response(
            status_code=200 if updated_count > 0 else 201,
            body={
                "exercises": result_exercises,
                "created": created_count,
                "updated": updated_count
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
        print(f"Error upserting exercises: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def get_exercises(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get all non-deleted exercises for a user.

    Returns all exercise check-ins for the authenticated user
    where deleted != True (or deleted attribute doesn't exist).

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with list of exercises
    """
    try:
        # Get table
        table_name = os.environ.get('EXERCISES_TABLE_NAME')
        if not table_name:
            raise ValueError("EXERCISES_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Query all exercises for this user
        response = table.query(
            KeyConditionExpression=Key('userId').eq(user_id)
        )

        exercises = response.get('Items', [])

        # Filter out deleted exercises
        # Only include exercises where deleted is not True
        non_deleted_exercises = [
            exercise for exercise in exercises
            if not exercise.get('deleted', False)
        ]

        print(f"Retrieved {len(non_deleted_exercises)} non-deleted exercises for user: {user_id}")

        return create_response(
            status_code=200,
            body={"exercises": non_deleted_exercises}
        )

    except Exception as e:
        print(f"Error getting exercises: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def delete_exercises(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Soft delete one or more exercises by setting deleted=True (batch support).

    Security: Uses userId from JWT token as partition key, ensuring users can
    only delete their own exercises. Items not found for this user are reported
    in notFoundIds.

    Expected request body:
    {
        "exerciseItemIds": ["uuid-string-1", "uuid-string-2", ...]
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response confirming deletions
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate exerciseItemIds array exists
        if 'exerciseItemIds' not in body:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "Request body must contain 'exerciseItemIds' array"
                }
            )

        exercise_item_ids = body['exerciseItemIds']

        if not isinstance(exercise_item_ids, list):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid format",
                    "message": "'exerciseItemIds' must be an array"
                }
            )

        if len(exercise_item_ids) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Empty exerciseItemIds array",
                    "message": "At least one exerciseItemId is required"
                }
            )

        # Get table
        table_name = os.environ.get('EXERCISES_TABLE_NAME')
        if not table_name:
            raise ValueError("EXERCISES_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # First, verify which items exist and belong to this user
        # This provides clear feedback about ownership before any modifications
        existing_items = {}
        for i in range(0, len(exercise_item_ids), 100):
            batch_ids = exercise_item_ids[i:i + 100]
            keys = [{'userId': user_id, 'exerciseItemId': eid} for eid in batch_ids]

            response = dynamodb.batch_get_item(
                RequestItems={
                    table_name: {
                        'Keys': keys
                    }
                }
            )

            for item in response.get('Responses', {}).get(table_name, []):
                existing_items[item['exerciseItemId']] = item

        # Determine which IDs don't exist for this user
        not_found_ids = [eid for eid in exercise_item_ids if eid not in existing_items]

        if not_found_ids:
            print(f"Items not found for user {user_id}: {not_found_ids}")

        # Get current datetime for lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Soft delete only the items that exist and belong to this user
        deleted_exercises = []

        for exercise_item_id in exercise_item_ids:
            if exercise_item_id not in existing_items:
                continue  # Skip items that don't exist for this user

            # Update item to set deleted=True and update lastModifiedDatetime
            response = table.update_item(
                Key={
                    'userId': user_id,
                    'exerciseItemId': exercise_item_id
                },
                UpdateExpression='SET deleted = :deleted, lastModifiedDatetime = :lastModified',
                ExpressionAttributeValues={
                    ':deleted': True,
                    ':lastModified': current_datetime
                },
                ReturnValues='ALL_NEW'
            )

            updated_item = response.get('Attributes', {})
            deleted_exercises.append(updated_item)

        print(f"Soft deleted {len(deleted_exercises)} exercises for user: {user_id}")

        # Build response
        response_body = {
            "message": f"Deleted {len(deleted_exercises)} exercise(s)",
            "deletedExercises": deleted_exercises
        }

        if not_found_ids:
            response_body["notFoundIds"] = not_found_ids

        return create_response(
            status_code=200,
            body=response_body
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
        print(f"Error deleting exercises: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


# =============================================================================
# Lift Set Operations
# =============================================================================

def create_lift_sets(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Create one or more lift sets (batch support).

    The userId from the JWT token is used as the partition key, ensuring users
    can only create their own lift sets.

    Expected request body:
    {
        "liftSets": [
            {
                "liftSetId": "uuid-string",
                "exerciseId": "uuid-string",
                "reps": 10,
                "weight": 135.5,
                "createdTimezone": "America/Los_Angeles",
                "createdDatetime": "2026-01-27T10:30:00.000Z"
            },
            ...
        ]
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with created lift sets
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate liftSets array exists
        if 'liftSets' not in body:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "Request body must contain 'liftSets' array"
                }
            )

        lift_sets_input = body['liftSets']

        if not isinstance(lift_sets_input, list):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid format",
                    "message": "'liftSets' must be an array"
                }
            )

        if len(lift_sets_input) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Empty liftSets array",
                    "message": "At least one lift set is required"
                }
            )

        # Validate each lift set
        required_fields = ['liftSetId', 'exerciseId', 'reps', 'weight', 'createdTimezone', 'createdDatetime']
        validation_errors = []

        for idx, lift_set in enumerate(lift_sets_input):
            missing_fields = [field for field in required_fields if field not in lift_set]
            if missing_fields:
                validation_errors.append(
                    f"Lift set at index {idx}: missing fields: {', '.join(missing_fields)}"
                )
                continue

            # Validate reps is a positive integer
            if not isinstance(lift_set['reps'], int) or lift_set['reps'] < 0:
                validation_errors.append(
                    f"Lift set at index {idx}: reps must be a non-negative integer"
                )

            # Validate weight is a number
            if not isinstance(lift_set['weight'], (int, float)):
                validation_errors.append(
                    f"Lift set at index {idx}: weight must be a number"
                )

        if validation_errors:
            return create_response(
                status_code=400,
                body={
                    "error": "Validation failed",
                    "message": "One or more lift sets have validation errors",
                    "errors": validation_errors
                }
            )

        # Get table
        table_name = os.environ.get('LIFT_SETS_TABLE_NAME')
        if not table_name:
            raise ValueError("LIFT_SETS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get current datetime
        current_datetime = get_current_datetime_iso()

        # Create all lift sets
        result_lift_sets = []

        for lift_set in lift_sets_input:
            lift_set_item = {
                'userId': user_id,
                'liftSetId': lift_set['liftSetId'],
                'exerciseId': lift_set['exerciseId'],
                'reps': lift_set['reps'],
                'weight': Decimal(str(lift_set['weight'])),  # Convert to Decimal for DynamoDB
                'createdTimezone': lift_set['createdTimezone'],
                'createdDatetime': lift_set['createdDatetime'],
                'lastModifiedDatetime': current_datetime,
            }

            table.put_item(Item=lift_set_item)

            # Convert Decimal back to float for JSON response
            response_item = {**lift_set_item, 'weight': float(lift_set_item['weight'])}
            result_lift_sets.append(response_item)

        print(f"Created {len(result_lift_sets)} lift sets for user {user_id}")

        return create_response(
            status_code=201,
            body={
                "liftSets": result_lift_sets,
                "created": len(result_lift_sets)
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
        print(f"Error creating lift sets: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def get_lift_sets(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get paginated lift sets for a user, most recent first.

    Uses the GSI (userId-createdDatetime-index) with ScanIndexForward=False
    to return lift sets ordered by createdDatetime descending.

    Query parameters:
    - limit: Number of items per page (default 100, max 500)
    - pageToken: Base64-encoded LastEvaluatedKey for pagination

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with paginated lift sets
    """
    try:
        # Get table
        table_name = os.environ.get('LIFT_SETS_TABLE_NAME')
        if not table_name:
            raise ValueError("LIFT_SETS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Parse query parameters
        query_params = event.get('queryStringParameters') or {}

        # Get limit (default 100, max 500)
        try:
            limit = int(query_params.get('limit', 100))
            limit = min(max(limit, 1), 500)  # Clamp between 1 and 500
        except ValueError:
            limit = 100

        # Get page token if provided
        page_token = query_params.get('pageToken')
        exclusive_start_key = None

        if page_token:
            try:
                decoded = base64.b64decode(page_token).decode('utf-8')
                exclusive_start_key = json.loads(decoded)
            except (ValueError, json.JSONDecodeError) as e:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid pageToken",
                        "message": "The provided pageToken is invalid"
                    }
                )

        # Build query parameters
        query_kwargs = {
            'IndexName': 'userId-createdDatetime-index',
            'KeyConditionExpression': Key('userId').eq(user_id),
            'ScanIndexForward': False,  # Most recent first
            'Limit': limit,
        }

        if exclusive_start_key:
            query_kwargs['ExclusiveStartKey'] = exclusive_start_key

        # Execute query
        response = table.query(**query_kwargs)

        lift_sets = response.get('Items', [])

        # Filter out deleted lift sets and convert Decimals for JSON serialization
        non_deleted_lift_sets = []
        for lift_set in lift_sets:
            if not lift_set.get('deleted', False):
                # Convert Decimal to appropriate types for JSON serialization
                if 'weight' in lift_set:
                    lift_set['weight'] = float(lift_set['weight'])
                if 'reps' in lift_set:
                    lift_set['reps'] = int(lift_set['reps'])
                non_deleted_lift_sets.append(lift_set)

        print(f"Retrieved {len(non_deleted_lift_sets)} non-deleted lift sets for user: {user_id}")

        # Build response
        response_body = {
            "liftSets": non_deleted_lift_sets,
            "count": len(non_deleted_lift_sets)
        }

        # Add pagination info if there are more results
        last_evaluated_key = response.get('LastEvaluatedKey')
        if last_evaluated_key:
            # Convert any Decimal values in the key for JSON serialization
            serializable_key = {
                k: float(v) if isinstance(v, Decimal) else v
                for k, v in last_evaluated_key.items()
            }
            # Encode the key as base64 for the page token
            encoded_key = base64.b64encode(
                json.dumps(serializable_key).encode('utf-8')
            ).decode('utf-8')
            response_body['nextPageToken'] = encoded_key
            response_body['hasMore'] = True
        else:
            response_body['hasMore'] = False

        return create_response(
            status_code=200,
            body=response_body
        )

    except Exception as e:
        print(f"Error getting lift sets: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def delete_lift_sets(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Soft delete one or more lift sets by setting deleted=True (batch support).

    Security: Uses userId from JWT token as partition key, ensuring users can
    only delete their own lift sets. Items not found for this user are reported
    in notFoundIds.

    Expected request body:
    {
        "liftSetIds": ["uuid-string-1", "uuid-string-2", ...]
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response confirming deletions
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate liftSetIds array exists
        if 'liftSetIds' not in body:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "Request body must contain 'liftSetIds' array"
                }
            )

        lift_set_ids = body['liftSetIds']

        if not isinstance(lift_set_ids, list):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid format",
                    "message": "'liftSetIds' must be an array"
                }
            )

        if len(lift_set_ids) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Empty liftSetIds array",
                    "message": "At least one liftSetId is required"
                }
            )

        # Get table
        table_name = os.environ.get('LIFT_SETS_TABLE_NAME')
        if not table_name:
            raise ValueError("LIFT_SETS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # First, verify which items exist and belong to this user
        existing_items = {}
        for i in range(0, len(lift_set_ids), 100):
            batch_ids = lift_set_ids[i:i + 100]
            keys = [{'userId': user_id, 'liftSetId': lid} for lid in batch_ids]

            response = dynamodb.batch_get_item(
                RequestItems={
                    table_name: {
                        'Keys': keys
                    }
                }
            )

            for item in response.get('Responses', {}).get(table_name, []):
                existing_items[item['liftSetId']] = item

        # Determine which IDs don't exist for this user
        not_found_ids = [lid for lid in lift_set_ids if lid not in existing_items]

        if not_found_ids:
            print(f"Lift sets not found for user {user_id}: {not_found_ids}")

        # Get current datetime for lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Soft delete only the items that exist and belong to this user
        deleted_lift_sets = []

        for lift_set_id in lift_set_ids:
            if lift_set_id not in existing_items:
                continue  # Skip items that don't exist for this user

            # Update item to set deleted=True and update lastModifiedDatetime
            response = table.update_item(
                Key={
                    'userId': user_id,
                    'liftSetId': lift_set_id
                },
                UpdateExpression='SET deleted = :deleted, lastModifiedDatetime = :lastModified',
                ExpressionAttributeValues={
                    ':deleted': True,
                    ':lastModified': current_datetime
                },
                ReturnValues='ALL_NEW'
            )

            updated_item = response.get('Attributes', {})
            # Convert Decimal to appropriate types for JSON serialization
            if 'weight' in updated_item:
                updated_item['weight'] = float(updated_item['weight'])
            if 'reps' in updated_item:
                updated_item['reps'] = int(updated_item['reps'])
            deleted_lift_sets.append(updated_item)

        print(f"Soft deleted {len(deleted_lift_sets)} lift sets for user: {user_id}")

        # Build response
        response_body = {
            "message": f"Deleted {len(deleted_lift_sets)} lift set(s)",
            "deletedLiftSets": deleted_lift_sets
        }

        if not_found_ids:
            response_body["notFoundIds"] = not_found_ids

        return create_response(
            status_code=200,
            body=response_body
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
        print(f"Error deleting lift sets: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


# =============================================================================
# Estimated 1RM Operations
# =============================================================================

def create_estimated_1rm(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Create one or more estimated 1RM records (batch support).

    The userId from the JWT token is used as the partition key, ensuring users
    can only create their own estimated 1RM records.

    Expected request body:
    {
        "estimated1RMs": [
            {
                "estimated1RMId": "uuid-string",
                "liftSetId": "uuid-of-lift-set",
                "exerciseId": "uuid-of-exercise",
                "value": 225.5,
                "createdTimezone": "America/Los_Angeles",
                "createdDatetime": "2026-01-27T10:30:00.000Z"
            },
            ...
        ]
    }

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with created estimated 1RM records
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate estimated1RMs array exists
        if 'estimated1RMs' not in body:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "Request body must contain 'estimated1RMs' array"
                }
            )

        estimated_1rms_input = body['estimated1RMs']

        if not isinstance(estimated_1rms_input, list):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid format",
                    "message": "'estimated1RMs' must be an array"
                }
            )

        if len(estimated_1rms_input) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Empty estimated1RMs array",
                    "message": "At least one estimated 1RM is required"
                }
            )

        # Validate each estimated 1RM
        required_fields = ['estimated1RMId', 'liftSetId', 'exerciseId', 'value', 'createdTimezone', 'createdDatetime']
        validation_errors = []

        for idx, e1rm in enumerate(estimated_1rms_input):
            missing_fields = [field for field in required_fields if field not in e1rm]
            if missing_fields:
                validation_errors.append(
                    f"Estimated 1RM at index {idx}: missing fields: {', '.join(missing_fields)}"
                )
                continue

            # Validate value is a number
            if not isinstance(e1rm['value'], (int, float)):
                validation_errors.append(
                    f"Estimated 1RM at index {idx}: value must be a number"
                )

        if validation_errors:
            return create_response(
                status_code=400,
                body={
                    "error": "Validation failed",
                    "message": "One or more estimated 1RMs have validation errors",
                    "errors": validation_errors
                }
            )

        # Get table
        table_name = os.environ.get('ESTIMATED_1RM_TABLE_NAME')
        if not table_name:
            raise ValueError("ESTIMATED_1RM_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get current datetime
        current_datetime = get_current_datetime_iso()

        # Create all estimated 1RM records
        result_estimated_1rms = []

        for e1rm in estimated_1rms_input:
            e1rm_item = {
                'userId': user_id,
                'liftSetId': e1rm['liftSetId'],  # Sort key
                'estimated1RMId': e1rm['estimated1RMId'],
                'exerciseId': e1rm['exerciseId'],
                'value': Decimal(str(e1rm['value'])),  # Convert to Decimal for DynamoDB
                'createdTimezone': e1rm['createdTimezone'],
                'createdDatetime': e1rm['createdDatetime'],
                'lastModifiedDatetime': current_datetime,
            }

            table.put_item(Item=e1rm_item)

            # Convert Decimal back to float for JSON response
            response_item = {**e1rm_item, 'value': float(e1rm_item['value'])}
            result_estimated_1rms.append(response_item)

        print(f"Created {len(result_estimated_1rms)} estimated 1RM records for user {user_id}")

        return create_response(
            status_code=201,
            body={
                "estimated1RMs": result_estimated_1rms,
                "created": len(result_estimated_1rms)
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
        print(f"Error creating estimated 1RM records: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def get_estimated_1rm(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Get paginated estimated 1RM records for a user, most recent first.

    Uses the GSI (userId-createdDatetime-index) with ScanIndexForward=False
    to return estimated 1RM records ordered by createdDatetime descending.

    Query parameters:
    - limit: Number of items per page (default 100, max 500)
    - pageToken: Base64-encoded LastEvaluatedKey for pagination

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response with paginated estimated 1RM records
    """
    try:
        # Get table
        table_name = os.environ.get('ESTIMATED_1RM_TABLE_NAME')
        if not table_name:
            raise ValueError("ESTIMATED_1RM_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Parse query parameters
        query_params = event.get('queryStringParameters') or {}

        # Get limit (default 100, max 500)
        try:
            limit = int(query_params.get('limit', 100))
            limit = min(max(limit, 1), 500)  # Clamp between 1 and 500
        except ValueError:
            limit = 100

        # Get page token if provided
        page_token = query_params.get('pageToken')
        exclusive_start_key = None

        if page_token:
            try:
                decoded = base64.b64decode(page_token).decode('utf-8')
                exclusive_start_key = json.loads(decoded)
            except (ValueError, json.JSONDecodeError) as e:
                return create_response(
                    status_code=400,
                    body={
                        "error": "Invalid pageToken",
                        "message": "The provided pageToken is invalid"
                    }
                )

        # Build query parameters
        query_kwargs = {
            'IndexName': 'userId-createdDatetime-index',
            'KeyConditionExpression': Key('userId').eq(user_id),
            'ScanIndexForward': False,  # Most recent first
            'Limit': limit,
        }

        if exclusive_start_key:
            query_kwargs['ExclusiveStartKey'] = exclusive_start_key

        # Execute query
        response = table.query(**query_kwargs)

        estimated_1rms = response.get('Items', [])

        # Filter out deleted records and convert Decimals to floats
        non_deleted_estimated_1rms = []
        for e1rm in estimated_1rms:
            if not e1rm.get('deleted', False):
                # Convert Decimal to float for JSON serialization
                if 'value' in e1rm:
                    e1rm['value'] = float(e1rm['value'])
                non_deleted_estimated_1rms.append(e1rm)

        print(f"Retrieved {len(non_deleted_estimated_1rms)} non-deleted estimated 1RM records for user: {user_id}")

        # Build response
        response_body = {
            "estimated1RMs": non_deleted_estimated_1rms,
            "count": len(non_deleted_estimated_1rms)
        }

        # Add pagination info if there are more results
        last_evaluated_key = response.get('LastEvaluatedKey')
        if last_evaluated_key:
            # Convert any Decimal values in the key for JSON serialization
            serializable_key = {
                k: float(v) if isinstance(v, Decimal) else v
                for k, v in last_evaluated_key.items()
            }
            # Encode the key as base64 for the page token
            encoded_key = base64.b64encode(
                json.dumps(serializable_key).encode('utf-8')
            ).decode('utf-8')
            response_body['nextPageToken'] = encoded_key
            response_body['hasMore'] = True
        else:
            response_body['hasMore'] = False

        return create_response(
            status_code=200,
            body=response_body
        )

    except Exception as e:
        print(f"Error getting estimated 1RM records: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )


def delete_estimated_1rm(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Soft delete one or more estimated 1RM records by setting deleted=True (batch support).

    Security: Uses userId from JWT token as partition key, ensuring users can
    only delete their own estimated 1RM records. Items not found for this user
    are reported in notFoundIds.

    Expected request body:
    {
        "liftSetIds": ["uuid-string-1", "uuid-string-2", ...]
    }

    Note: We use liftSetIds because liftSetId is the sort key for this table.

    Args:
        event: API Gateway event
        user_id: User ID from JWT token

    Returns:
        API Gateway response confirming deletions
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate liftSetIds array exists
        if 'liftSetIds' not in body:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "Request body must contain 'liftSetIds' array"
                }
            )

        lift_set_ids = body['liftSetIds']

        if not isinstance(lift_set_ids, list):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid format",
                    "message": "'liftSetIds' must be an array"
                }
            )

        if len(lift_set_ids) == 0:
            return create_response(
                status_code=400,
                body={
                    "error": "Empty liftSetIds array",
                    "message": "At least one liftSetId is required"
                }
            )

        # Get table
        table_name = os.environ.get('ESTIMATED_1RM_TABLE_NAME')
        if not table_name:
            raise ValueError("ESTIMATED_1RM_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # First, verify which items exist and belong to this user
        existing_items = {}
        for i in range(0, len(lift_set_ids), 100):
            batch_ids = lift_set_ids[i:i + 100]
            keys = [{'userId': user_id, 'liftSetId': lid} for lid in batch_ids]

            response = dynamodb.batch_get_item(
                RequestItems={
                    table_name: {
                        'Keys': keys
                    }
                }
            )

            for item in response.get('Responses', {}).get(table_name, []):
                existing_items[item['liftSetId']] = item

        # Determine which IDs don't exist for this user
        not_found_ids = [lid for lid in lift_set_ids if lid not in existing_items]

        if not_found_ids:
            print(f"Estimated 1RM records not found for user {user_id}: {not_found_ids}")

        # Get current datetime for lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Soft delete only the items that exist and belong to this user
        deleted_estimated_1rms = []

        for lift_set_id in lift_set_ids:
            if lift_set_id not in existing_items:
                continue  # Skip items that don't exist for this user

            # Update item to set deleted=True and update lastModifiedDatetime
            response = table.update_item(
                Key={
                    'userId': user_id,
                    'liftSetId': lift_set_id
                },
                UpdateExpression='SET deleted = :deleted, lastModifiedDatetime = :lastModified',
                ExpressionAttributeValues={
                    ':deleted': True,
                    ':lastModified': current_datetime
                },
                ReturnValues='ALL_NEW'
            )

            updated_item = response.get('Attributes', {})
            # Convert Decimal to float for JSON serialization
            if 'value' in updated_item:
                updated_item['value'] = float(updated_item['value'])
            deleted_estimated_1rms.append(updated_item)

        print(f"Soft deleted {len(deleted_estimated_1rms)} estimated 1RM records for user: {user_id}")

        # Build response
        response_body = {
            "message": f"Deleted {len(deleted_estimated_1rms)} estimated 1RM record(s)",
            "deletedEstimated1RMs": deleted_estimated_1rms
        }

        if not_found_ids:
            response_body["notFoundIds"] = not_found_ids

        return create_response(
            status_code=200,
            body=response_body
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
        print(f"Error deleting estimated 1RM records: {str(e)}")
        import traceback
        traceback.print_exc()
        return create_response(
            status_code=500,
            body={"message": "Internal server error"}
        )
