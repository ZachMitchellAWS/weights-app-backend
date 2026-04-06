"""Datetime utilities for checkin service."""

from datetime import datetime, timezone


def get_current_datetime_iso() -> str:
    """
    Get current datetime in ISO 8601 format (UTC).

    Returns:
        ISO 8601 formatted datetime string
    """
    return datetime.now(timezone.utc).isoformat()
