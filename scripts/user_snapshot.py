#!/usr/bin/env python3
"""Save and load user data snapshots from DynamoDB staging tables.

This script allows saving all data for a specific user across all DynamoDB tables
to a JSON file, and loading it back. Useful for preserving test user state.

Usage:
    python scripts/user_snapshot.py save
    python scripts/user_snapshot.py load
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

# Configuration
USER_ID = "18dee8ea-ac11-4b02-ae52-670cb830e44a"
REGION = "us-west-1"
ENV = "staging"
PROJECT = "liftthebull"

# Output file location
SNAPSHOT_DIR = Path(__file__).parent.parent / "snapshots"
SNAPSHOT_FILE = SNAPSHOT_DIR / f"user_{USER_ID[:8]}.json"

# Tables and their key schemas
# Format: (table_name_suffix, partition_key, sort_key or None)
TABLES = [
    ("users", "userId", None),
    ("user-properties", "userId", None),
    ("password-reset-codes", "userId", None),
    ("exercises", "userId", "exerciseItemId"),
    ("lift-sets", "userId", "liftSetId"),
    ("estimated-1rm", "userId", "liftSetId"),
    ("sequences", "userId", "sequenceId"),
]


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


def save_user_data():
    """Save all user data from staging tables to a JSON file."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)

    snapshot = {
        "user_id": USER_ID,
        "tables": {}
    }

    total_items = 0

    for table_suffix, pk_name, sk_name in TABLES:
        table_name = get_table_name(table_suffix)
        table = dynamodb.Table(table_name)

        print(f"Querying {table_name}...")

        # Query for all items with this user ID
        response = table.query(
            KeyConditionExpression=Key(pk_name).eq(USER_ID)
        )

        items = response.get("Items", [])

        # Handle pagination if needed
        while "LastEvaluatedKey" in response:
            response = table.query(
                KeyConditionExpression=Key(pk_name).eq(USER_ID),
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))

        snapshot["tables"][table_suffix] = items
        total_items += len(items)
        print(f"  Found {len(items)} item(s)")

    # Ensure snapshot directory exists
    SNAPSHOT_DIR.mkdir(exist_ok=True)

    # Write to file
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2, cls=DecimalEncoder)

    print(f"\nSaved {total_items} total items to {SNAPSHOT_FILE}")


def load_user_data():
    """Load user data from JSON file back into staging tables."""
    if not SNAPSHOT_FILE.exists():
        print(f"Error: Snapshot file not found: {SNAPSHOT_FILE}")
        print("Run 'make save-user-staging' first to create a snapshot.")
        sys.exit(1)

    dynamodb = boto3.resource("dynamodb", region_name=REGION)

    # Read snapshot file
    with open(SNAPSHOT_FILE, "r") as f:
        snapshot = json.load(f)

    if snapshot.get("user_id") != USER_ID:
        print(f"Error: Snapshot user ID mismatch")
        print(f"  Expected: {USER_ID}")
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

        # Use batch writer for efficiency
        with table.batch_writer() as batch:
            for item in items:
                # Recursively convert all floats to Decimal
                converted_item = convert_floats_to_decimal(item)
                batch.put_item(Item=converted_item)

        total_items += len(items)

    print(f"\nLoaded {total_items} total items from {SNAPSHOT_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Save and load user data snapshots from DynamoDB staging tables."
    )
    parser.add_argument(
        "action",
        choices=["save", "load"],
        help="Action to perform: 'save' exports data, 'load' imports data"
    )

    args = parser.parse_args()

    if args.action == "save":
        save_user_data()
    elif args.action == "load":
        load_user_data()


if __name__ == "__main__":
    main()
