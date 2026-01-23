"""JWT token generation and validation utilities.

Security best practices implemented:
- Short-lived access tokens (15 minutes)
- Long-lived refresh tokens (30 days)
- Secret key from SSM Parameter Store
- Proper JWT claims (sub, exp, iat, type)
- Signature validation
- Expiration validation
- Token type validation

Production recommendations:
- Implement token rotation
- Add jti (JWT ID) for token revocation
- Consider using RS256 (asymmetric) for microservices
"""

import jwt
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

# Import SSM utility for reading secrets
from utils.ssm import get_jwt_secret_key


# Token expiration times
ACCESS_TOKEN_EXPIRATION_MINUTES = 15  # Short-lived access tokens
REFRESH_TOKEN_EXPIRATION_DAYS = 30    # Long-lived refresh tokens

# JWT Algorithm
ALGORITHM = "HS256"


def get_secret_key() -> str:
    """
    Get JWT secret key from SSM Parameter Store.

    The secret key is cached in memory after first retrieval to avoid
    repeated SSM API calls during Lambda warm starts.

    Returns:
        JWT secret key

    Raises:
        ValueError: If JWT_SECRET_KEY_PARAM is not set or parameter not found
        Exception: If SSM API call fails
    """
    return get_jwt_secret_key()


def generate_access_token(user_id: str, email_address: str) -> str:
    """
    Generate a short-lived access token.

    The access token is used for API authentication and expires quickly
    to minimize the impact of token theft.

    Args:
        user_id: User's unique identifier
        email_address: User's email address

    Returns:
        Encoded JWT access token

    Raises:
        ValueError: If JWT_SECRET_KEY is not set
    """
    secret_key = get_secret_key()

    now = datetime.utcnow()
    expiration = now + timedelta(minutes=ACCESS_TOKEN_EXPIRATION_MINUTES)

    payload = {
        "sub": user_id,                    # Subject: user ID
        "email": email_address,            # User's email
        "type": "access",                  # Token type
        "iat": now,                        # Issued at
        "exp": expiration,                 # Expiration time
    }

    token = jwt.encode(payload, secret_key, algorithm=ALGORITHM)
    return token


def generate_refresh_token(user_id: str) -> str:
    """
    Generate a long-lived refresh token.

    The refresh token is used to obtain new access tokens without
    requiring the user to log in again.

    Security notes:
    - Refresh tokens should be stored securely (httpOnly cookies in browsers)
    - Consider storing refresh token hash in database for revocation
    - Implement token rotation: issue new refresh token on each use

    Args:
        user_id: User's unique identifier

    Returns:
        Encoded JWT refresh token

    Raises:
        ValueError: If JWT_SECRET_KEY is not set
    """
    secret_key = get_secret_key()

    now = datetime.utcnow()
    expiration = now + timedelta(days=REFRESH_TOKEN_EXPIRATION_DAYS)

    payload = {
        "sub": user_id,                    # Subject: user ID
        "type": "refresh",                 # Token type
        "iat": now,                        # Issued at
        "exp": expiration,                 # Expiration time
    }

    token = jwt.encode(payload, secret_key, algorithm=ALGORITHM)
    return token


def generate_token_pair(user_id: str, email_address: str) -> Tuple[str, str]:
    """
    Generate both access and refresh tokens.

    Args:
        user_id: User's unique identifier
        email_address: User's email address

    Returns:
        Tuple of (access_token, refresh_token)

    Raises:
        ValueError: If JWT_SECRET_KEY is not set
    """
    access_token = generate_access_token(user_id, email_address)
    refresh_token = generate_refresh_token(user_id)
    return access_token, refresh_token


def validate_token(token: str, expected_type: str = "access") -> Optional[Dict]:
    """
    Validate and decode a JWT token.

    Performs comprehensive validation:
    - Signature verification
    - Expiration check
    - Token type verification

    Args:
        token: JWT token to validate
        expected_type: Expected token type ("access" or "refresh")

    Returns:
        Decoded token payload if valid, None otherwise

    Examples:
        >>> payload = validate_token(access_token, "access")
        >>> if payload:
        ...     user_id = payload["sub"]
        ...     email = payload["email"]
    """
    try:
        secret_key = get_secret_key()

        # Decode and validate token
        # This automatically checks:
        # - Signature validity
        # - Expiration time
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[ALGORITHM]
        )

        # Verify token type
        token_type = payload.get("type")
        if token_type != expected_type:
            print(f"Token type mismatch: expected {expected_type}, got {token_type}")
            return None

        return payload

    except jwt.ExpiredSignatureError:
        print("Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        print(f"Invalid token: {str(e)}")
        return None
    except Exception as e:
        print(f"Error validating token: {str(e)}")
        return None


def validate_access_token(token: str) -> Optional[Dict]:
    """
    Validate an access token.

    Args:
        token: Access token to validate

    Returns:
        Decoded token payload if valid, None otherwise
    """
    return validate_token(token, expected_type="access")


def validate_refresh_token(token: str) -> Optional[Dict]:
    """
    Validate a refresh token.

    Args:
        token: Refresh token to validate

    Returns:
        Decoded token payload if valid, None otherwise
    """
    return validate_token(token, expected_type="refresh")


def get_token_expiration_time(token_type: str = "access") -> int:
    """
    Get token expiration time in seconds.

    Args:
        token_type: Type of token ("access" or "refresh")

    Returns:
        Expiration time in seconds
    """
    if token_type == "access":
        return ACCESS_TOKEN_EXPIRATION_MINUTES * 60
    elif token_type == "refresh":
        return REFRESH_TOKEN_EXPIRATION_DAYS * 24 * 60 * 60
    else:
        raise ValueError(f"Unknown token type: {token_type}")
