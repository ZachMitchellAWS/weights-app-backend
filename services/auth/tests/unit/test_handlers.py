"""
Unit tests for auth service Lambda handlers.

These tests verify the Lambda handler logic without requiring AWS resources.
We test request validation, response formatting, and error handling.

Test patterns demonstrated:
- Testing missing required fields
- Testing successful responses
- Testing error handling
- Using stub implementations

To run tests:
    pytest services/auth/tests/unit -v
"""

import json
import pytest
import sys
from pathlib import Path

# Add Lambda code to path for imports
lambda_path = Path(__file__).parent.parent.parent / "lambda"
sys.path.insert(0, str(lambda_path))

from handlers.login import handler as login_handler
from handlers.register import handler as register_handler


class TestLoginHandler:
    """Test cases for the login handler."""

    def test_login_missing_email(self):
        """Test login with missing email returns 400."""
        event = {
            "body": json.dumps({
                "password": "test123"
            })
        }
        context = {}

        response = login_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "Missing required fields" in body["error"]

    def test_login_missing_password(self):
        """Test login with missing password returns 400."""
        event = {
            "body": json.dumps({
                "email": "test@example.com"
            })
        }
        context = {}

        response = login_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_login_missing_both_credentials(self):
        """Test login with missing both email and password returns 400."""
        event = {
            "body": json.dumps({})
        }
        context = {}

        response = login_handler(event, context)

        assert response["statusCode"] == 400

    def test_login_stub_success(self):
        """Test login with valid credentials returns 200 with stub token."""
        event = {
            "body": json.dumps({
                "email": "test@example.com",
                "password": "password123"
            })
        }
        context = {}

        response = login_handler(event, context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "token" in body
        assert body["token"] == "stub-token-123"
        assert "user" in body
        assert body["user"]["email"] == "test@example.com"

    def test_login_invalid_json(self):
        """Test login with invalid JSON returns 400."""
        event = {
            "body": "invalid json{"
        }
        context = {}

        response = login_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Invalid JSON" in body["error"]

    def test_login_response_has_cors_headers(self):
        """Test that login response includes CORS headers."""
        event = {
            "body": json.dumps({
                "email": "test@example.com",
                "password": "password123"
            })
        }
        context = {}

        response = login_handler(event, context)

        assert "headers" in response
        assert "Access-Control-Allow-Origin" in response["headers"]
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"


class TestRegisterHandler:
    """Test cases for the register handler."""

    def test_register_missing_email(self):
        """Test register with missing email returns 400."""
        event = {
            "body": json.dumps({
                "password": "test123",
                "name": "Test User"
            })
        }
        context = {}

        response = register_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "Missing required fields" in body["error"]

    def test_register_missing_password(self):
        """Test register with missing password returns 400."""
        event = {
            "body": json.dumps({
                "email": "test@example.com",
                "name": "Test User"
            })
        }
        context = {}

        response = register_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_register_missing_name(self):
        """Test register with missing name returns 400."""
        event = {
            "body": json.dumps({
                "email": "test@example.com",
                "password": "test123"
            })
        }
        context = {}

        response = register_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_register_stub_success(self):
        """Test register with valid data returns 201."""
        event = {
            "body": json.dumps({
                "email": "newuser@example.com",
                "password": "securepass123",
                "name": "New User"
            })
        }
        context = {}

        response = register_handler(event, context)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert "user" in body
        assert body["user"]["email"] == "newuser@example.com"
        assert body["user"]["name"] == "New User"
        assert "created_at" in body["user"]
        # Should not return password
        assert "password" not in body["user"]

    def test_register_invalid_json(self):
        """Test register with invalid JSON returns 400."""
        event = {
            "body": "not valid json"
        }
        context = {}

        response = register_handler(event, context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Invalid JSON" in body["error"]

    def test_register_response_has_cors_headers(self):
        """Test that register response includes CORS headers."""
        event = {
            "body": json.dumps({
                "email": "test@example.com",
                "password": "password123",
                "name": "Test User"
            })
        }
        context = {}

        response = register_handler(event, context)

        assert "headers" in response
        assert "Access-Control-Allow-Origin" in response["headers"]
        assert "Content-Type" in response["headers"]
