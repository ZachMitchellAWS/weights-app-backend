"""Auth handler for create-user, login, refresh, and password reset endpoints."""

import json
import os
import uuid
import re
import secrets
from typing import Dict, Any
from datetime import datetime, timedelta
import traceback
import boto3
from boto3.dynamodb.conditions import Key

# Import from parent directory (Lambda function structure)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.response import create_response, get_current_datetime_iso
from utils.jwt_utils import (
    generate_token_pair,
    validate_refresh_token,
    validate_access_token,
    generate_access_token,
    get_token_expiration_time,
)
from utils.password import hash_password, verify_password
from utils.apple_auth import verify_apple_identity_token
from utils.sentry_init import init_sentry, set_sentry_user
import sentry_sdk

init_sentry()

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Unified handler for both API requests and Lambda Authorizer requests.

    Detects the event type and routes accordingly:

    **Lambda Authorizer Mode** (when called by API Gateway for authentication):
    - Event contains: authorizationToken, methodArn
    - Validates JWT access token
    - Returns IAM policy (Allow/Deny)

    **API Mode** (when called by API Gateway for endpoints):
    - POST /auth/create-user: Create new user with emailAddress and password
    - POST /auth/login: Authenticate user and return JWT credentials
    - POST /auth/refresh: Refresh access token using refresh token
    - POST /auth/logout: Remove refresh token (requires authentication)
    - POST /auth/initiate-password-reset: Generate and send password reset code
    - POST /auth/confirm-password-reset: Validate code and reset password

    Args:
        event: API Gateway event or Lambda Authorizer event
        context: Lambda context object

    Returns:
        API Gateway response or IAM policy document
    """
    try:
        # Detect if this is an authorizer request or API request
        # Authorizer events have 'authorizationToken' and 'methodArn'
        # API Gateway events have 'path', 'httpMethod', etc.
        if "authorizationToken" in event and "methodArn" in event:
            # This is a Lambda Authorizer request
            print("Handling Lambda Authorizer request")
            return handle_authorizer_request(event)

        # This is a regular API request
        # Get the path from the API Gateway event
        path = event.get("path", "")
        print(f"Handling API request for path: {path}")

        # Route to appropriate handler based on path
        if path.endswith("/create-user"):
            return handle_create_user(event)
        elif path.endswith("/login"):
            return handle_login(event)
        elif path.endswith("/refresh"):
            return handle_refresh(event)
        elif path.endswith("/logout"):
            return handle_logout(event)
        elif path.endswith("/initiate-password-reset"):
            return handle_initiate_password_reset(event)
        elif path.endswith("/confirm-password-reset"):
            return handle_confirm_password_reset(event)
        elif path.endswith("/apple-signin"):
            return handle_apple_signin(event)
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
        print(f"Unexpected error in handler: {str(e)}")
        print(traceback.format_exc())

        # Check if this was an authorizer request
        if "authorizationToken" in event and "methodArn" in event:
            # Return Deny policy for authorizer errors
            return generate_policy("user", "Deny", event.get("methodArn", ""))

        # Return 500 for API request errors
        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "An unexpected error occurred"
            }
        )


def handle_create_user(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /create-user requests.

    Creates a new user with emailAddress and password (stored in plain text).
    Returns JWT access and refresh tokens upon successful creation.

    Request body:
        {
            "emailAddress": "user@example.com",
            "password": "password123"
        }

    Response:
        {
            "userId": "generated-uuid",
            "emailAddress": "user@example.com",
            "accessToken": "jwt-access-token",
            "refreshToken": "jwt-refresh-token",
            "accessTokenExpiresIn": 900,
            "refreshTokenExpiresIn": 2592000
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        email_address = body.get("emailAddress")
        password = body.get("password")

        # Validate required fields
        if not email_address or not password:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required fields",
                    "message": "Both emailAddress and password are required"
                }
            )

        # Get table name and index from environment
        table_name = os.environ.get("USERS_TABLE_NAME")
        email_index_name = os.environ.get("EMAIL_INDEX_NAME")

        if not table_name:
            raise ValueError("USERS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Check if user with this email already exists (query GSI)
        response = table.query(
            IndexName=email_index_name,
            KeyConditionExpression=Key('emailAddress').eq(email_address)
        )

        if response.get('Items'):
            return create_response(
                status_code=409,
                body={
                    "error": "User already exists",
                    "message": f"A user with email {email_address} already exists"
                }
            )

        # Generate new userId
        user_id = str(uuid.uuid4())

        # Hash password before storing
        # This uses bcrypt with automatic salt generation
        password_hash = hash_password(password)
        print(f"Password hashed successfully for user creation")

        # Generate JWT token pair
        access_token, refresh_token = generate_token_pair(user_id, email_address)

        # Get current datetime for createdDatetime and lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Create user item with hashed password
        user_item = {
            "userId": user_id,
            "emailAddress": email_address,
            "passwordHash": password_hash,  # Store bcrypt hash, not plaintext
            "createdDatetime": current_datetime,  # Track when user was created
            "lastModifiedDatetime": current_datetime,  # Initially same as createdDatetime
        }

        # Write to DynamoDB
        table.put_item(Item=user_item)

        # Create user_properties item
        # Get user_properties table name from environment
        user_properties_table_name = os.environ.get("USER_PROPERTIES_TABLE_NAME")
        if user_properties_table_name:
            user_properties_table = dynamodb.Table(user_properties_table_name)
            user_properties_item = {
                "userId": user_id,
                "availableChangePlates": [],  # Empty list by default
                "bodyweight": 200,  # Default 200 lbs — updated during onboarding
                "biologicalSex": "male",  # Default male — updated during onboarding
                "hasMetStrengthTierConditions": False,
                "createdDatetime": current_datetime,
                "lastModifiedDatetime": current_datetime,
            }
            user_properties_table.put_item(Item=user_properties_item)
            print(f"Created user_properties for user: {user_id}")
        else:
            print("Warning: USER_PROPERTIES_TABLE_NAME not set, skipping user_properties creation")

        print(f"Created user: {user_id} with email: {email_address}")

        # Send welcome email asynchronously
        email_lambda_arn = os.environ.get("EMAIL_LAMBDA_ARN")
        if email_lambda_arn:
            try:
                lambda_client = boto3.client('lambda')
                email_payload = {
                    'emailAddress': email_address,
                    'templateType': 'welcome',
                    'variables': {}  # No variables needed for welcome email
                }

                lambda_client.invoke(
                    FunctionName=email_lambda_arn,
                    InvocationType='Event',  # Async invocation
                    Payload=json.dumps(email_payload)
                )
                print(f"Welcome email Lambda invoked for user {user_id}")

            except Exception as e:
                print(f"Error invoking welcome email Lambda: {str(e)}")
                # Don't fail user creation if email fails
        else:
            print("Warning: EMAIL_LAMBDA_ARN not set, skipping welcome email")

        # Return success with real JWT tokens
        return create_response(
            status_code=201,
            body={
                "userId": user_id,
                "emailAddress": email_address,
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "accessTokenExpiresIn": get_token_expiration_time("access"),
                "refreshTokenExpiresIn": get_token_expiration_time("refresh")
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
        print(f"Error in handle_create_user: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to create user"
            }
        )


def handle_login(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /login requests.

    Looks up user by emailAddress using GSI, verifies password match,
    and returns JWT access and refresh tokens on success.

    Request body:
        {
            "emailAddress": "user@example.com",
            "password": "password123"
        }

    Response:
        {
            "userId": "user-uuid",
            "emailAddress": "user@example.com",
            "accessToken": "jwt-access-token",
            "refreshToken": "jwt-refresh-token",
            "accessTokenExpiresIn": 900,
            "refreshTokenExpiresIn": 2592000
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        email_address = body.get("emailAddress")
        password = body.get("password")

        # Validate required fields
        if not email_address or not password:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required fields",
                    "message": "Both emailAddress and password are required"
                }
            )

        # Get table name and index from environment
        table_name = os.environ.get("USERS_TABLE_NAME")
        email_index_name = os.environ.get("EMAIL_INDEX_NAME")

        if not table_name:
            raise ValueError("USERS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Query user by emailAddress using GSI
        response = table.query(
            IndexName=email_index_name,
            KeyConditionExpression=Key('emailAddress').eq(email_address)
        )

        items = response.get('Items', [])

        if not items:
            return create_response(
                status_code=401,
                body={
                    "error": "Authentication failed",
                    "message": "Invalid email or password"
                }
            )

        # Get the first matching user (should only be one)
        user = items[0]

        # Get stored password hash
        stored_password_hash = user.get("passwordHash")
        if not stored_password_hash:
            # Handle legacy users that might still have plaintext passwords
            # This should not happen in production after migration
            print(f"Warning: User {user.get('userId')} has no passwordHash field")
            return create_response(
                status_code=401,
                body={
                    "error": "Authentication failed",
                    "message": "Invalid email or password"
                }
            )

        # Verify password against stored hash
        if not verify_password(password, stored_password_hash):
            print(f"Password verification failed for user: {email_address}")
            return create_response(
                status_code=401,
                body={
                    "error": "Authentication failed",
                    "message": "Invalid email or password"
                }
            )

        # Password matches - generate new JWT token pair
        user_id = user.get("userId")
        access_token, refresh_token = generate_token_pair(user_id, email_address)

        print(f"User {user_id} logged in successfully")

        # Return success with real JWT tokens
        return create_response(
            status_code=200,
            body={
                "userId": user_id,
                "emailAddress": email_address,
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "accessTokenExpiresIn": get_token_expiration_time("access"),
                "refreshTokenExpiresIn": get_token_expiration_time("refresh")
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
        print(f"Error in handle_login: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to authenticate user"
            }
        )


def handle_refresh(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /refresh requests.

    Validates the provided refresh token (stateless - JWT signature and expiration only)
    and issues a new access token.

    Request body:
        {
            "refreshToken": "jwt-refresh-token"
        }

    Response:
        {
            "userId": "user-uuid",
            "accessToken": "new-jwt-access-token",
            "accessTokenExpiresIn": 900
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        refresh_token = body.get("refreshToken")

        # Validate required field
        if not refresh_token:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "refreshToken is required"
                }
            )

        # Validate refresh token JWT signature and expiration (stateless)
        payload = validate_refresh_token(refresh_token)
        if not payload:
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid token",
                    "message": "Refresh token is invalid or expired"
                }
            )

        # Extract user ID from token
        user_id = payload.get("sub")
        if not user_id:
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid token",
                    "message": "Refresh token is malformed"
                }
            )

        # Get table name from environment
        table_name = os.environ.get("USERS_TABLE_NAME")
        if not table_name:
            raise ValueError("USERS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Get user from DynamoDB to get email for new access token
        response = table.get_item(Key={"userId": user_id})
        user = response.get("Item")

        if not user:
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid token",
                    "message": "User not found"
                }
            )

        # Generate new access token
        email_address = user.get("emailAddress")
        new_access_token = generate_access_token(user_id, email_address)

        print(f"Refreshed access token for user: {user_id}")

        # Return new access token
        return create_response(
            status_code=200,
            body={
                "userId": user_id,
                "accessToken": new_access_token,
                "accessTokenExpiresIn": get_token_expiration_time("access")
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
        print(f"Error in handle_refresh: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to refresh token"
            }
        )


def handle_logout(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /logout requests.

    With stateless refresh tokens, this endpoint serves as a signal to the client
    to discard tokens locally. No server-side state is modified.

    Note: The refresh token remains valid until its natural expiration (30 days).
    True revocation would require stateful token storage.

    This endpoint requires authentication via the Lambda Authorizer.

    Response:
        {
            "message": "Logged out successfully"
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Get user ID from authorizer context
        # The Lambda Authorizer adds this to the request context
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

        print(f"User {user_id} logged out (client should discard tokens)")

        # Return success - client should discard tokens locally
        return create_response(
            status_code=200,
            body={
                "message": "Logged out successfully"
            }
        )

    except Exception as e:
        print(f"Error in handle_logout: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to logout"
            }
        )


def handle_authorizer_request(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Lambda Authorizer requests for validating JWT access tokens.

    This function is called when the main handler detects an authorizer event.
    It validates the access token and returns an IAM policy allowing or denying the request.

    The Authorization header should contain: "Bearer <access-token>"

    Args:
        event: Lambda Authorizer event (contains authorizationToken and methodArn)

    Returns:
        IAM policy document with authorization decision
    """
    try:
        # Extract token from Authorization header
        # Event structure for Lambda Authorizer is different from API Gateway
        token = event.get("authorizationToken", "")

        # Remove "Bearer " prefix if present
        if token.startswith("Bearer "):
            token = token[7:]

        # Extract the method ARN to return in the policy
        method_arn = event.get("methodArn", "")

        print(f"Authorizer called for method: {method_arn}")
        print(f"Token (first 20 chars): {token[:20]}...")

        # Validate the access token
        payload = validate_access_token(token)

        if not payload:
            print("Token validation failed")
            # Return Deny policy
            return generate_policy("user", "Deny", method_arn)

        # Extract user information from token
        user_id = payload.get("sub")
        email = payload.get("email")

        print(f"Token validated for user: {user_id}")

        # Return Allow policy with user context
        # The context is passed to the main handler in requestContext.authorizer
        return generate_policy(
            user_id,
            "Allow",
            method_arn,
            context={
                "userId": user_id,
                "email": email
            }
        )

    except Exception as e:
        print(f"Error in authorizer_handler: {str(e)}")
        print(traceback.format_exc())

        # Return Deny policy on any error
        return generate_policy("user", "Deny", event.get("methodArn", ""))


def generate_policy(principal_id: str, effect: str, resource: str, context: Dict[str, str] = None) -> Dict[str, Any]:
    """
    Generate an IAM policy document for API Gateway.

    This policy allows or denies access to the API endpoint.

    Args:
        principal_id: User identifier (typically user ID)
        effect: "Allow" or "Deny"
        resource: Method ARN from the authorizer event
        context: Optional context to pass to the main handler

    Returns:
        IAM policy document
    """
    auth_response = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }

    # Add context if provided
    # This context is accessible in the main handler via requestContext.authorizer
    if context:
        auth_response["context"] = context

    return auth_response


def handle_initiate_password_reset(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /initiate-password-reset requests.

    Security features:
    - Always returns success (doesn't reveal if email exists)
    - Rate limiting: max 3 attempts per hour
    - 6-digit random code
    - Code expires in 1 hour (auto-deleted via TTL)
    - Async email delivery (doesn't block response)

    Request body:
        {
            "emailAddress": "user@example.com"
        }

    Response (always success):
        {
            "message": "If an account exists for this email, a reset code has been sent"
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response (always 200)
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        email_address = body.get("emailAddress")

        # Validate email format
        if not email_address or not is_valid_email(email_address):
            # Always return success to prevent email enumeration
            return create_response(
                status_code=200,
                body={
                    "message": "If an account exists for this email, a reset code has been sent"
                }
            )

        # Get table names from environment
        users_table_name = os.environ.get("USERS_TABLE_NAME")
        email_index_name = os.environ.get("EMAIL_INDEX_NAME")
        reset_codes_table_name = os.environ.get("PASSWORD_RESET_CODES_TABLE_NAME")
        email_lambda_arn = os.environ.get("EMAIL_LAMBDA_ARN")

        if not all([users_table_name, email_index_name, reset_codes_table_name, email_lambda_arn]):
            raise ValueError("Missing required environment variables")

        users_table = dynamodb.Table(users_table_name)
        reset_codes_table = dynamodb.Table(reset_codes_table_name)

        # Look up user by email
        response = users_table.query(
            IndexName=email_index_name,
            KeyConditionExpression=Key('emailAddress').eq(email_address)
        )

        users = response.get('Items', [])

        # If user doesn't exist, return success anyway (security)
        if not users:
            print(f"Password reset requested for non-existent email (security: returning success)")
            return create_response(
                status_code=200,
                body={
                    "message": "If an account exists for this email, a reset code has been sent"
                }
            )

        user = users[0]
        user_id = user.get('userId')

        # Check rate limiting and determine attempt count
        reset_attempts = 1  # Default for first attempt
        try:
            existing_code = reset_codes_table.get_item(Key={'userId': user_id}).get('Item')
            if existing_code:
                existing_attempts = existing_code.get('resetAttempts', 0)
                created_time = datetime.fromisoformat(existing_code.get('createdDatetime'))
                time_diff = datetime.utcnow() - created_time

                # If less than 1 hour, increment attempts from existing code
                if time_diff < timedelta(hours=1):
                    reset_attempts = existing_attempts + 1

                    # If already at 3+ attempts, rate limit
                    if existing_attempts >= 3:
                        print(f"Rate limit exceeded for user {user_id} (attempt {reset_attempts})")
                        # Still return success (security)
                        return create_response(
                            status_code=200,
                            body={
                                "message": "If an account exists for this email, a reset code has been sent"
                            }
                        )
                # If >= 1 hour has passed, reset to 1 (new attempt window)
                else:
                    reset_attempts = 1
                    print(f"Previous reset code expired, starting new attempt window for user {user_id}")
        except Exception as e:
            print(f"Error checking rate limit: {str(e)}")
            # Continue anyway with reset_attempts = 1

        # Generate 6-digit code
        reset_code = generate_reset_code()

        # Calculate expiry time (1 hour from now)
        current_datetime = get_current_datetime_iso()
        expiry_time = int((datetime.utcnow() + timedelta(hours=1)).timestamp())

        # Store reset code in DynamoDB
        # This will replace any existing code for this user
        reset_code_item = {
            'userId': user_id,
            'code': reset_code,
            'createdDatetime': current_datetime,
            'expiryTime': expiry_time,  # TTL attribute
            'resetAttempts': reset_attempts,  # Incremented based on existing attempts
        }

        reset_codes_table.put_item(Item=reset_code_item)
        print(f"Password reset code generated for user {user_id}")

        # Invoke email Lambda asynchronously
        try:
            lambda_client = boto3.client('lambda')
            email_payload = {
                'emailAddress': email_address,
                'templateType': 'password-reset',
                'variables': {
                    'PASSWORD_RESET_CODE': reset_code,
                    'EXPIRY_TIME': '1 hour'
                }
            }

            lambda_client.invoke(
                FunctionName=email_lambda_arn,
                InvocationType='Event',  # Async invocation
                Payload=json.dumps(email_payload)
            )
            print(f"Email Lambda invoked for user {user_id}")

        except Exception as e:
            print(f"Error invoking email Lambda: {str(e)}")
            # Don't fail the request - code is still valid

        # Always return success
        return create_response(
            status_code=200,
            body={
                "message": "If an account exists for this email, a reset code has been sent"
            }
        )

    except json.JSONDecodeError:
        # Return success even on invalid JSON (security)
        return create_response(
            status_code=200,
            body={
                "message": "If an account exists for this email, a reset code has been sent"
            }
        )
    except Exception as e:
        print(f"Error in handle_initiate_password_reset: {str(e)}")
        print(traceback.format_exc())

        # Still return success (security)
        return create_response(
            status_code=200,
            body={
                "message": "If an account exists for this email, a reset code has been sent"
            }
        )


def handle_confirm_password_reset(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /confirm-password-reset requests.

    Validates the reset code and updates the user's password.
    The code must be valid (exists, not expired, matches).

    Request body:
        {
            "emailAddress": "user@example.com",
            "code": "123456",
            "newPassword": "newpassword123"
        }

    Response:
        {
            "message": "Password reset successfully"
        }

    Args:
        event: API Gateway event

    Returns:
        API Gateway response
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        email_address = body.get("emailAddress")
        code = body.get("code")
        new_password = body.get("newPassword")

        # Validate required fields
        if not email_address or not code or not new_password:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required fields",
                    "message": "emailAddress, code, and newPassword are required"
                }
            )

        # Validate email format
        if not is_valid_email(email_address):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid email",
                    "message": "Email address format is invalid"
                }
            )

        # Validate code format (6 digits)
        if not re.match(r'^\d{6}$', code):
            return create_response(
                status_code=400,
                body={
                    "error": "Invalid code",
                    "message": "Reset code must be 6 digits"
                }
            )

        # Get table names from environment
        users_table_name = os.environ.get("USERS_TABLE_NAME")
        email_index_name = os.environ.get("EMAIL_INDEX_NAME")
        reset_codes_table_name = os.environ.get("PASSWORD_RESET_CODES_TABLE_NAME")

        if not all([users_table_name, email_index_name, reset_codes_table_name]):
            raise ValueError("Missing required environment variables")

        users_table = dynamodb.Table(users_table_name)
        reset_codes_table = dynamodb.Table(reset_codes_table_name)

        # Look up user by email
        response = users_table.query(
            IndexName=email_index_name,
            KeyConditionExpression=Key('emailAddress').eq(email_address)
        )

        users = response.get('Items', [])

        if not users:
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid reset code",
                    "message": "The reset code is invalid or has expired"
                }
            )

        user = users[0]
        user_id = user.get('userId')

        # Get reset code from table
        reset_code_response = reset_codes_table.get_item(Key={'userId': user_id})
        reset_code_item = reset_code_response.get('Item')

        if not reset_code_item:
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid reset code",
                    "message": "The reset code is invalid or has expired"
                }
            )

        # Verify code matches
        stored_code = reset_code_item.get('code')
        if stored_code != code:
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid reset code",
                    "message": "The reset code is invalid or has expired"
                }
            )

        # Check if code is expired (should be auto-deleted by TTL, but check anyway)
        created_time = datetime.fromisoformat(reset_code_item.get('createdDatetime'))
        time_diff = datetime.utcnow() - created_time

        if time_diff > timedelta(hours=1):
            # Code expired - delete it
            reset_codes_table.delete_item(Key={'userId': user_id})
            return create_response(
                status_code=401,
                body={
                    "error": "Invalid reset code",
                    "message": "The reset code is invalid or has expired"
                }
            )

        # Hash new password
        password_hash = hash_password(new_password)

        # Get current datetime for lastModifiedDatetime
        current_datetime = get_current_datetime_iso()

        # Update user's password
        users_table.update_item(
            Key={"userId": user_id},
            UpdateExpression="SET passwordHash = :hash, lastModifiedDatetime = :datetime",
            ExpressionAttributeValues={
                ":hash": password_hash,
                ":datetime": current_datetime
            }
        )

        # Delete reset code (consumed)
        reset_codes_table.delete_item(Key={'userId': user_id})

        print(f"Password reset successful for user {user_id}")

        return create_response(
            status_code=200,
            body={
                "message": "Password reset successfully"
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
        print(f"Error in handle_confirm_password_reset: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to reset password"
            }
        )


def handle_apple_signin(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle POST /apple-signin requests.

    Three-case user resolution:
    1. Lookup by Apple sub ID → login (existing Apple user)
    2. Fallback by email → link Apple ID to existing account
    3. No match → create new account

    Request body:
        {
            "identityToken": "apple-jwt-token",
            "authorizationCode": "apple-auth-code",
            "email": "user@example.com" (optional),
            "fullName": "John Doe" (optional)
        }

    Response:
        {
            "userId": "...",
            "emailAddress": "...",
            "accessToken": "...",
            "refreshToken": "...",
            "accessTokenExpiresIn": 900,
            "refreshTokenExpiresIn": 2592000,
            "isNewUser": true/false
        }
    """
    try:
        # Parse request body
        body = json.loads(event.get("body", "{}"))
        identity_token = body.get("identityToken")
        email_from_client = body.get("email")
        full_name = body.get("fullName")

        if not identity_token:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing required field",
                    "message": "identityToken is required"
                }
            )

        # Verify Apple identity token
        try:
            apple_payload = verify_apple_identity_token(identity_token)
        except Exception as e:
            print(f"Apple token verification failed: {str(e)}")
            return create_response(
                status_code=401,
                body={
                    "error": "Authentication failed",
                    "message": "Invalid Apple identity token"
                }
            )

        apple_sub = apple_payload.get("sub")
        apple_email = apple_payload.get("email") or email_from_client

        if not apple_sub:
            return create_response(
                status_code=401,
                body={
                    "error": "Authentication failed",
                    "message": "Apple identity token missing sub claim"
                }
            )

        # Get table names and indexes from environment
        table_name = os.environ.get("USERS_TABLE_NAME")
        email_index_name = os.environ.get("EMAIL_INDEX_NAME")
        apple_user_id_index_name = os.environ.get("APPLE_USER_ID_INDEX_NAME")

        if not table_name:
            raise ValueError("USERS_TABLE_NAME environment variable not set")

        table = dynamodb.Table(table_name)

        # Case 1: Lookup by Apple sub ID
        if apple_user_id_index_name:
            response = table.query(
                IndexName=apple_user_id_index_name,
                KeyConditionExpression=Key('appleUserId').eq(apple_sub)
            )
            items = response.get('Items', [])

            if items:
                user = items[0]
                user_id = user.get("userId")
                email_address = user.get("emailAddress")

                access_token, refresh_token = generate_token_pair(user_id, email_address)
                print(f"Apple Sign In: existing user login for {user_id}")

                return create_response(
                    status_code=200,
                    body={
                        "userId": user_id,
                        "emailAddress": email_address,
                        "accessToken": access_token,
                        "refreshToken": refresh_token,
                        "accessTokenExpiresIn": get_token_expiration_time("access"),
                        "refreshTokenExpiresIn": get_token_expiration_time("refresh"),
                        "isNewUser": False,
                    }
                )

        # Case 2: Lookup by email
        if apple_email and email_index_name:
            response = table.query(
                IndexName=email_index_name,
                KeyConditionExpression=Key('emailAddress').eq(apple_email)
            )
            items = response.get('Items', [])

            if items:
                user = items[0]
                user_id = user.get("userId")
                email_address = user.get("emailAddress")

                # Link Apple sub to existing account
                current_datetime = get_current_datetime_iso()
                table.update_item(
                    Key={"userId": user_id},
                    UpdateExpression="SET appleUserId = :asub, lastModifiedDatetime = :dt",
                    ExpressionAttributeValues={
                        ":asub": apple_sub,
                        ":dt": current_datetime,
                    }
                )

                access_token, refresh_token = generate_token_pair(user_id, email_address)
                print(f"Apple Sign In: linked Apple ID to existing user {user_id}")

                return create_response(
                    status_code=200,
                    body={
                        "userId": user_id,
                        "emailAddress": email_address,
                        "accessToken": access_token,
                        "refreshToken": refresh_token,
                        "accessTokenExpiresIn": get_token_expiration_time("access"),
                        "refreshTokenExpiresIn": get_token_expiration_time("refresh"),
                        "isNewUser": False,
                    }
                )

        # Case 3: Create new account
        if not apple_email:
            return create_response(
                status_code=400,
                body={
                    "error": "Missing email",
                    "message": "Email is required for new account creation"
                }
            )

        user_id = str(uuid.uuid4())
        current_datetime = get_current_datetime_iso()

        user_item = {
            "userId": user_id,
            "emailAddress": apple_email,
            "appleUserId": apple_sub,
            "createdDatetime": current_datetime,
            "lastModifiedDatetime": current_datetime,
        }

        if full_name:
            user_item["fullName"] = full_name

        table.put_item(Item=user_item)

        # Create user_properties item
        user_properties_table_name = os.environ.get("USER_PROPERTIES_TABLE_NAME")
        if user_properties_table_name:
            user_properties_table = dynamodb.Table(user_properties_table_name)
            user_properties_item = {
                "userId": user_id,
                "availableChangePlates": [],
                "hasMetStrengthTierConditions": False,
                "createdDatetime": current_datetime,
                "lastModifiedDatetime": current_datetime,
            }
            user_properties_table.put_item(Item=user_properties_item)
            print(f"Created user_properties for Apple user: {user_id}")

        # Send welcome email
        email_lambda_arn = os.environ.get("EMAIL_LAMBDA_ARN")
        if email_lambda_arn:
            try:
                lambda_client = boto3.client('lambda')
                email_payload = {
                    'emailAddress': apple_email,
                    'templateType': 'welcome',
                    'variables': {}
                }
                lambda_client.invoke(
                    FunctionName=email_lambda_arn,
                    InvocationType='Event',
                    Payload=json.dumps(email_payload)
                )
                print(f"Welcome email Lambda invoked for Apple user {user_id}")
            except Exception as e:
                print(f"Error invoking welcome email Lambda: {str(e)}")

        access_token, refresh_token = generate_token_pair(user_id, apple_email)
        print(f"Apple Sign In: created new user {user_id} with email {apple_email}")

        return create_response(
            status_code=201,
            body={
                "userId": user_id,
                "emailAddress": apple_email,
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "accessTokenExpiresIn": get_token_expiration_time("access"),
                "refreshTokenExpiresIn": get_token_expiration_time("refresh"),
                "isNewUser": True,
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
        print(f"Error in handle_apple_signin: {str(e)}")
        print(traceback.format_exc())

        return create_response(
            status_code=500,
            body={
                "error": "Internal server error",
                "message": "Failed to authenticate with Apple"
            }
        )


def is_valid_email(email: str) -> bool:
    """
    Validate email address format using regex.

    Args:
        email: Email address to validate

    Returns:
        True if valid, False otherwise
    """
    # Basic email regex pattern
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(email_pattern, email))


def generate_reset_code() -> str:
    """
    Generate a random 6-digit reset code.

    Uses secrets module for cryptographically strong random numbers.

    Returns:
        6-digit code as string (e.g., "123456")
    """
    # Generate random number between 0 and 999999, pad with zeros
    code_number = secrets.randbelow(1000000)
    return f"{code_number:06d}"
