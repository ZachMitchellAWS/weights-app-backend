#!/usr/bin/env python3
"""Clear all items from production DynamoDB tables.

Uses batch_writer for efficient bulk deletes (up to 25 items per API call)
instead of individual delete-item CLI calls.

Usage:
    python scripts/clear_production_db.py
"""

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

REGION = "us-west-1"
ENV = "production"
PROJECT = "liftthebull"

# Tables: (suffix, partition_key, sort_key or None)
TABLES = [
    ("users", "userId", None),
    ("user-properties", "userId", None),
    ("password-reset-codes", "userId", None),
    ("exercises", "userId", "exerciseItemId"),
    ("lift-sets", "userId", "liftSetId"),
    ("estimated-1rm", "userId", "liftSetId"),
    ("splits", "userId", "splitId"),
    ("entitlement-grants", "userId", "startUtc"),
    ("subscription-events", "userId", "eventTimestamp"),
    ("set-plan-templates", "userId", "templateId"),
    ("accessory-goal-checkins", "userId", "checkinId"),
]


def get_table_name(suffix: str) -> str:
    return f"{PROJECT}-{ENV}-{suffix}"


def clear_all_tables():
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    total_deleted = 0

    for suffix, pk, sk in TABLES:
        table_name = get_table_name(suffix)
        table = dynamodb.Table(table_name)
        print(f"Clearing {table_name}...")

        # Scan all items, projecting only key attributes
        key_attrs = [pk] if sk is None else [pk, sk]
        scan_kwargs = {"ProjectionExpression": ", ".join(key_attrs)}

        try:
            items = []
            while True:
                response = table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    break
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                print(f"  (table does not exist, skipping)")
                continue
            raise

        if not items:
            print(f"  (empty)")
            continue

        # Batch delete - batch_writer handles chunking into groups of 25
        with table.batch_writer() as batch:
            for item in items:
                key = {pk: item[pk]}
                if sk:
                    key[sk] = item[sk]
                batch.delete_item(Key=key)

        print(f"  Deleted {len(items)} item(s)")
        total_deleted += len(items)

    print(f"\nDone. Deleted {total_deleted} total items.")


if __name__ == "__main__":
    clear_all_tables()
