"""AWS Systems Manager Parameter Store utilities.

This module provides functions to read secrets from SSM Parameter Store.
Values are cached in memory to avoid repeated API calls during Lambda warm starts.
"""

import os
import boto3
from typing import Dict, Optional

# Initialize SSM client
ssm_client = boto3.client('ssm')

# In-memory cache for SSM parameters (persists across warm Lambda invocations)
_parameter_cache: Dict[str, str] = {}


def get_parameter(parameter_name: str, use_cache: bool = True) -> Optional[str]:
    """
    Get a parameter value from SSM Parameter Store.

    Parameters are cached in memory to improve performance. The cache
    persists across Lambda warm starts but is cleared on cold starts.

    Args:
        parameter_name: SSM parameter name (path)
        use_cache: Whether to use cached value (default: True)

    Returns:
        Parameter value, or None if not found

    Raises:
        Exception: If SSM API call fails
    """
    # Return cached value if available
    if use_cache and parameter_name in _parameter_cache:
        return _parameter_cache[parameter_name]

    try:
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=True  # Decrypt SecureString parameters
        )
        value = response['Parameter']['Value']

        # Cache the value
        _parameter_cache[parameter_name] = value

        return value

    except ssm_client.exceptions.ParameterNotFound:
        print(f"SSM parameter not found: {parameter_name}")
        return None
    except Exception as e:
        print(f"Error getting SSM parameter {parameter_name}: {str(e)}")
        raise


def get_jwt_secret_key() -> str:
    """
    Get JWT secret key from SSM Parameter Store.

    The parameter name is read from the JWT_SECRET_KEY_PARAM environment variable.

    Returns:
        JWT secret key value

    Raises:
        ValueError: If parameter name env var is not set
        Exception: If SSM parameter cannot be read
    """
    param_name = os.environ.get("JWT_SECRET_KEY_PARAM")
    if not param_name:
        raise ValueError("JWT_SECRET_KEY_PARAM environment variable is not set")

    secret_key = get_parameter(param_name)
    if not secret_key:
        raise ValueError(f"JWT secret key not found in SSM: {param_name}")

    return secret_key


def get_password_pepper() -> str:
    """
    Get password pepper from SSM Parameter Store.

    The parameter name is read from the PASSWORD_PEPPER_PARAM environment variable.

    Returns:
        Password pepper value (empty string if not found)
    """
    param_name = os.environ.get("PASSWORD_PEPPER_PARAM")
    if not param_name:
        print("PASSWORD_PEPPER_PARAM environment variable is not set")
        return ""

    pepper = get_parameter(param_name)
    if not pepper:
        print(f"Password pepper not found in SSM: {param_name}")
        return ""

    return pepper
