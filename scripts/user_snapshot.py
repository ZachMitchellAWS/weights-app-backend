#!/usr/bin/env python3
"""Save and load user data snapshots from DynamoDB staging tables.

This script allows saving all data for a specific user across all DynamoDB tables
to a JSON file, and loading it back. Useful for preserving test user state.

On load, missing fields are filled with schema defaults (migration), so snapshots
taken before a schema change will load correctly.

Usage:
    python scripts/user_snapshot.py save
    python scripts/user_snapshot.py load
    python scripts/user_snapshot.py save --user-id 1702dad4-e9af-42db-9bfa-7204cd69d54a
    python scripts/user_snapshot.py load --user-id 1702dad4-e9af-42db-9bfa-7204cd69d54a
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# Configuration
DEFAULT_USER_ID = "18dee8ea-ac11-4b02-ae52-670cb830e44a"
REGION = "us-west-1"
ENV = "staging"
PROJECT = "liftthebull"

# Output file location
SNAPSHOT_DIR = Path(__file__).parent.parent / "snapshots"

# Tables and their key schemas
# Format: (table_name_suffix, partition_key, sort_key or None)
TABLES = [
    ("users", "userId", None),
    ("user-properties", "userId", None),
    ("password-reset-codes", "userId", None),
    ("exercises", "userId", "exerciseItemId"),
    ("lift-sets", "userId", "liftSetId"),
    ("estimated-1rm", "userId", "liftSetId"),
    ("entitlement-grants", "userId", "startUtc"),
    ("subscription-events", "userId", "eventTimestamp"),
    ("set-plans", "userId", "planId"),
    ("accessory-goal-checkins", "userId", "checkinId"),
    ("groups", "userId", "groupId"),
]

# Schema defaults for migration on load.
# Only tables/fields that need defaults are listed here.
# Fields not listed are left as-is (schema-agnostic passthrough).
_NOW_SENTINEL = "<<use_existing_or_now>>"

TABLE_SCHEMAS = {
    "user-properties": {
        "availableChangePlates": [],
        "createdDatetime": _NOW_SENTINEL,
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
    "exercises": {
        "isCustom": False,
        "loadType": "Barbell",
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
    "lift-sets": {
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
    "estimated-1rm": {
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
    "set-plans": {
        "isCustom": False,
        "effortSequence": [],
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
    "accessory-goal-checkins": {
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
    "groups": {
        "isCustom": False,
        "exerciseIds": [],
        "sortOrder": 0,
        "lastModifiedDatetime": _NOW_SENTINEL,
    },
}


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types from DynamoDB."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            # Convert to int if whole number, else float
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def convert_floats_to_decimal(obj):
    """Recursively convert all floats to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_floats_to_decimal(item) for item in obj]
    return obj


def get_table_name(suffix: str) -> str:
    """Get full table name for staging environment."""
    return f"{PROJECT}-{ENV}-{suffix}"


def get_snapshot_file(user_id: str) -> Path:
    """Get snapshot file path for a given user ID."""
    return SNAPSHOT_DIR / f"user_{user_id[:8]}.json"


def apply_schema_defaults(table_suffix, item):
    """Add missing fields with defaults. Mutates item in place.

    Returns set of field names that were added, or empty set.
    """
    schema = TABLE_SCHEMAS.get(table_suffix)
    if not schema:
        return set()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    added = set()
    for field, default in schema.items():
        if field not in item:
            if default == _NOW_SENTINEL:
                item[field] = now
            else:
                item[field] = default
            added.add(field)
    return added


def save_user_data(user_id: str):
    """Save all user data from staging tables to a JSON file."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    snapshot_file = get_snapshot_file(user_id)

    snapshot = {
        "user_id": user_id,
        "tables": {}
    }

    total_items = 0

    for table_suffix, pk_name, sk_name in TABLES:
        table_name = get_table_name(table_suffix)
        table = dynamodb.Table(table_name)

        print(f"Querying {table_name}...")

        try:
            # Query for all items with this user ID
            response = table.query(
                KeyConditionExpression=Key(pk_name).eq(user_id)
            )

            items = response.get("Items", [])

            # Handle pagination if needed
            while "LastEvaluatedKey" in response:
                response = table.query(
                    KeyConditionExpression=Key(pk_name).eq(user_id),
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                items.extend(response.get("Items", []))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                print(f"  (table does not exist, skipping)")
                continue
            raise

        snapshot["tables"][table_suffix] = items
        total_items += len(items)
        print(f"  Found {len(items)} item(s)")

    # Ensure snapshot directory exists
    SNAPSHOT_DIR.mkdir(exist_ok=True)

    # Write to file
    with open(snapshot_file, "w") as f:
        json.dump(snapshot, f, indent=2, cls=DecimalEncoder)

    print(f"\nSaved {total_items} total items to {snapshot_file}")


def load_user_data(user_id: str):
    """Load user data from JSON file back into staging tables."""
    snapshot_file = get_snapshot_file(user_id)

    if not snapshot_file.exists():
        print(f"Error: Snapshot file not found: {snapshot_file}")
        print("Run 'make save-user-staging' first to create a snapshot.")
        sys.exit(1)

    dynamodb = boto3.resource("dynamodb", region_name=REGION)

    # Read snapshot file
    with open(snapshot_file, "r") as f:
        snapshot = json.load(f)

    if snapshot.get("user_id") != user_id:
        print(f"Error: Snapshot user ID mismatch")
        print(f"  Expected: {user_id}")
        print(f"  Found: {snapshot.get('user_id')}")
        sys.exit(1)

    total_items = 0

    for table_suffix, pk_name, sk_name in TABLES:
        table_name = get_table_name(table_suffix)
        table = dynamodb.Table(table_name)

        items = snapshot["tables"].get(table_suffix, [])
        if not items:
            print(f"Skipping {table_name} (no items in snapshot)")
            continue

        print(f"Loading {len(items)} item(s) into {table_name}...")

        # Apply schema migration and track what was added
        migrated_count = 0
        all_added_fields = set()

        for item in items:
            added = apply_schema_defaults(table_suffix, item)
            if added:
                migrated_count += 1
                all_added_fields.update(added)

        if migrated_count > 0:
            print(f"  Migrated {migrated_count} item(s): added [{', '.join(sorted(all_added_fields))}]")

        # Use batch writer for efficiency
        with table.batch_writer() as batch:
            for item in items:
                # Recursively convert all floats to Decimal
                converted_item = convert_floats_to_decimal(item)
                batch.put_item(Item=converted_item)

        total_items += len(items)

    print(f"\nLoaded {total_items} total items from {snapshot_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Save and load user data snapshots from DynamoDB staging tables."
    )
    parser.add_argument(
        "action",
        choices=["save", "load"],
        help="Action to perform: 'save' exports data, 'load' imports data"
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help=f"User ID to save/load (default: {DEFAULT_USER_ID})"
    )

    args = parser.parse_args()

    if args.action == "save":
        save_user_data(args.user_id)
    elif args.action == "load":
        load_user_data(args.user_id)


if __name__ == "__main__":
    main()
