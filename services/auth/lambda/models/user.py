"""User data model."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


class User(BaseModel):
    """
    User model for authentication.

    Attributes:
        email: User's email address (used as partition key in DynamoDB)
        name: User's display name
        password_hash: Hashed password (never store plaintext passwords)
        created_at: Account creation timestamp
        updated_at: Last update timestamp
    """
    email: EmailStr
    name: str
    password_hash: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        """Pydantic configuration."""
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
