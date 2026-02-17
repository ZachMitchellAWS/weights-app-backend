"""Apple Sign In token verification utilities.

Verifies Apple identity tokens (JWTs) by:
- Fetching Apple's public keys (JWKS) from https://appleid.apple.com/auth/keys
- Validating RS256 signature against Apple's public key
- Checking issuer, audience, and expiration claims
"""

import os
import time
import jwt
from jwt import PyJWKClient
from typing import Dict, Optional


# Cache Apple's JWKS client to avoid re-fetching keys on every request
_jwks_client: Optional[PyJWKClient] = None
_jwks_client_created_at: float = 0
JWKS_CACHE_TTL_SECONDS = 3600  # Re-create client every hour

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"


def _get_jwks_client() -> PyJWKClient:
    """Get or create a cached PyJWKClient for Apple's JWKS endpoint."""
    global _jwks_client, _jwks_client_created_at

    now = time.time()
    if _jwks_client is None or (now - _jwks_client_created_at) > JWKS_CACHE_TTL_SECONDS:
        _jwks_client = PyJWKClient(APPLE_JWKS_URL)
        _jwks_client_created_at = now

    return _jwks_client


def verify_apple_identity_token(identity_token: str) -> Dict:
    """
    Verify and decode an Apple identity token.

    Validates:
    - RS256 signature against Apple's public keys (JWKS)
    - Issuer is https://appleid.apple.com
    - Audience matches our app's bundle ID
    - Token is not expired

    Args:
        identity_token: The JWT identity token from Apple Sign In

    Returns:
        Decoded token payload containing sub, email, email_verified, etc.

    Raises:
        ValueError: If APPLE_BUNDLE_ID env var is not set
        jwt.InvalidTokenError: If token verification fails
    """
    bundle_ids_raw = os.environ.get("APPLE_BUNDLE_ID")
    if not bundle_ids_raw:
        raise ValueError("APPLE_BUNDLE_ID environment variable not set")

    # Support comma-separated bundle IDs (e.g. production and staging)
    allowed_audiences = [b.strip() for b in bundle_ids_raw.split(",")]

    client = _get_jwks_client()
    signing_key = client.get_signing_key_from_jwt(identity_token)

    payload = jwt.decode(
        identity_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=allowed_audiences,
        issuer=APPLE_ISSUER,
    )

    return payload
