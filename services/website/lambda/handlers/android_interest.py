"""Android-interest form Lambda handler.

Handles POST /website/android-interest — public endpoint (no auth, no API key).
Writes submissions to the android-interest DynamoDB table.

Bot mitigation (both server-validated; clients can't bypass):
  1. Honeypot — request must NOT include a non-empty `company_url` field.
  2. Time-on-page — request must include `formMs >= MIN_FORM_MS` (a human
     can't realistically submit faster).
If either check trips, the handler returns the same 200 success payload as
a real submission (so bots can't tell their attempt failed) but writes
nothing to DynamoDB.
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
import boto3
from utils.sentry_init import init_sentry

init_sentry()

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["ANDROID_INTEREST_TABLE_NAME"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Anti-bot thresholds.
MIN_FORM_MS = 1500  # humans take ≥ ~1.5s to fill three fields

# Field length caps — drop oversized payloads silently to avoid storage DoS.
MAX_LEN = {
    "email": 200,
    "name": 100,
    "comment": 1000,
    "company_url": 200,
}

SUCCESS_BODY = {"message": "Thanks — your interest is recorded."}


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _silent_ok():
    """Bot-mitigation drop. Same payload as a real success so bots can't
    discover that their submission failed."""
    return _response(200, SUCCESS_BODY)


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    # Field length caps before anything else.
    for key, limit in MAX_LEN.items():
        value = body.get(key)
        if isinstance(value, str) and len(value) > limit:
            return _silent_ok()

    # Honeypot check.
    if (body.get("company_url") or "").strip():
        return _silent_ok()

    # Time-on-page check.
    try:
        form_ms = int(body.get("formMs", 0))
    except (TypeError, ValueError):
        form_ms = 0
    if form_ms < MIN_FORM_MS:
        return _silent_ok()

    email = (body.get("email") or "").strip()
    if not email or not EMAIL_RE.match(email):
        return _response(400, {"error": "Valid email is required"})

    name = (body.get("name") or "").strip()
    comment = (body.get("comment") or "").strip()

    submission_id = str(uuid.uuid4())
    created_at = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )

    headers = event.get("headers") or {}
    user_agent = headers.get("User-Agent") or headers.get("user-agent") or ""
    request_context = (event.get("requestContext") or {}).get("identity") or {}
    source_ip = request_context.get("sourceIp", "")

    item = {
        "submissionId": submission_id,
        "email": email,
        "name": name,
        "comment": comment,
        "createdAt": created_at,
        "userAgent": user_agent,
        "sourceIp": source_ip,
    }

    table.put_item(Item=item)

    return _response(200, SUCCESS_BODY)
