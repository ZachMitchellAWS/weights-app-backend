"""Simple password hashing using SHA256 with pepper.

This implementation uses only Python standard library.
The pepper is a secret string stored in SSM Parameter Store
that is appended to passwords before hashing.

Note: This implementation does not use salt, so identical passwords
will produce identical hashes. For production use, consider adding
per-user salt or using bcrypt/argon2.

Security recommendations:
- Implement rate limiting on login endpoints
- Consider account lockout after failed attempts
"""

import hashlib
import hmac

# Import SSM utility for reading secrets
from utils.ssm import get_password_pepper as get_pepper_from_ssm


def get_pepper() -> str:
    """
    Get password pepper from SSM Parameter Store.

    The pepper is cached in memory after first retrieval to avoid
    repeated SSM API calls during Lambda warm starts.

    Returns:
        Pepper string (empty string if not set)
    """
    return get_pepper_from_ssm()


def hash_password(password: str, pepper: str = None) -> str:
    """
    Hash a password using SHA256 with pepper.

    The password is combined with a pepper (secret string), then hashed
    using SHA256.

    Args:
        password: Plain text password to hash
        pepper: Optional secret pepper (uses env var if not provided)

    Returns:
        Hash string (64 character hex string)

    Example:
        >>> hashed = hash_password("mypassword")
        >>> print(hashed)
        a1b2c3d4...
    """
    if pepper is None:
        pepper = get_pepper()

    # Combine password with pepper
    password_with_pepper = password + pepper

    # Hash with SHA256
    hash_obj = hashlib.sha256(password_with_pepper.encode('utf-8'))

    # Return hash as hex string
    return hash_obj.hexdigest()


def verify_password(password: str, hashed_password: str, pepper: str = None) -> bool:
    """
    Verify a password against a stored hash.

    Args:
        password: Plain text password to verify
        hashed_password: Hash string to verify against
        pepper: Optional secret pepper (uses env var if not provided)

    Returns:
        True if password matches hash, False otherwise

    Example:
        >>> hashed = hash_password("mypassword")
        >>> verify_password("mypassword", hashed)
        True
        >>> verify_password("wrongpassword", hashed)
        False
    """
    if pepper is None:
        pepper = get_pepper()

    try:
        # Hash the provided password
        computed_hash = hash_password(password, pepper)

        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(computed_hash, hashed_password)

    except Exception as e:
        print(f"Error verifying password: {str(e)}")
        return False
