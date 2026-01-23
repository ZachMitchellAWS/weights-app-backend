"""Lambda handler functions for auth service."""

from .auth import handler as auth_handler

__all__ = ["auth_handler"]
