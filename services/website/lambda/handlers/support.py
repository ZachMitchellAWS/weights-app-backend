"""Support form Lambda handler.

Handles POST /website/support — public endpoint (no auth, no API key).
Validates required fields, writes to DynamoDB support-tickets table.
"""

import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
import boto3
from utils.sentry_init import init_sentry
import sentry_sdk

init_sentry()

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["SUPPORT_TICKETS_TABLE_NAME"])

REQUIRED_FIELDS = ["firstName", "lastName", "email", "reason", "message"]

VALID_REASONS = [
    "Account issue",
    "Billing / subscription",
    "Bug report",
    "Feature request",
    "Data question",
    "Other",
]


def create_response(status_code, body):
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


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {"error": "Invalid JSON body"})

    # Validate required fields
    missing = [f for f in REQUIRED_FIELDS if not body.get(f, "").strip()]
    if missing:
        return create_response(400, {"error": f"Missing fields: {', '.join(missing)}"})

    reason = body["reason"].strip()
    if reason not in VALID_REASONS:
        return create_response(400, {"error": f"Invalid reason. Must be one of: {', '.join(VALID_REASONS)}"})

    ticket_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    item = {
        "ticketId": ticket_id,
        "firstName": body["firstName"].strip(),
        "lastName": body["lastName"].strip(),
        "email": body["email"].strip(),
        "reason": reason,
        "message": body["message"].strip(),
        "createdAt": created_at,
    }

    table.put_item(Item=item)

    return create_response(200, {"message": "Your message has been sent. We'll be in touch soon."})
