#!/usr/bin/env python3
"""Delete ALL DynamoDB data for a specific user across every per-user table.

This is a destructive, irreversible operation. There is no undo. Run
`make save-user-production USER_ID=<uuid>` first if you want a snapshot
to restore from.

The table inventory below is the authoritative list of every table whose
partition key is `userId`. It was derived by walking every CDK stack in
`services/*/infrastructure/*.py`. Tables NOT in this list (support-tickets,
android-interest) are intentionally excluded — they aren't userId-partitioned.

If a new userId-partitioned table is added in the future, add it here too.

Usage:
    python scripts/delete_user.py --env staging    --user-id <uuid>
    python scripts/delete_user.py --env production --user-id <uuid>
    python scripts/delete_user.py --env staging    --user-id <uuid> --dry-run

Both --env and --user-id are REQUIRED. There is no default user, by design.
"""

import argparse
import re
import sys
from typing import Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGION = "us-west-1"
PROJECT = "liftthebull"

# UUID format — accepts canonical 8-4-4-4-12 hex with hyphens.
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# === Authoritative table inventory ==========================================
# Format: (table_suffix, partition_key, sort_key_or_None, source_stack)
#
# Source stack column is for human reference when chasing down where a table
# is declared. Keep this list in sync with the CDK stacks under
# services/*/infrastructure/.
#
# Excluded intentionally:
#   - support-tickets   (PK=ticketId, not userId)
#   - android-interest  (PK=submissionId, public form submissions)
TABLES: List[Tuple[str, str, Optional[str], str]] = [
    ("users",                     "userId", None,              "auth"),
    ("password-reset-codes",      "userId", None,              "auth"),
    ("user-properties",           "userId", None,              "user"),
    ("account-deletion-requests", "userId", None,              "user"),
    ("feedback",                  "userId", "createdDatetime", "user"),
    ("ad-attributions",           "userId", "createdDatetime", "user"),
    ("generated-sessions",        "userId", "sessionId",       "sessions"),
    ("exercises",                 "userId", "exerciseItemId",  "checkin"),
    ("lift-sets",                 "userId", "liftSetId",       "checkin"),
    ("estimated-1rm",             "userId", "liftSetId",       "checkin"),
    ("set-plans",                 "userId", "planId",          "checkin"),
    ("accessory-goal-checkins",   "userId", "checkinId",       "checkin"),
    ("groups",                    "userId", "groupId",         "checkin"),
    ("entitlement-grants",        "userId", "startUtc",        "entitlements"),
    ("subscription-events",       "userId", "eventTimestamp",  "entitlements"),
    ("insight-tasks",             "userId", "insightWeek",     "insights"),
    ("insights-cache",            "userId", "insightWeek",     "insights"),
]


def table_name(env: str, suffix: str) -> str:
    return f"{PROJECT}-{env}-{suffix}"


def query_all_items(table, pk_name: str, user_id: str) -> List[Dict]:
    """Query a table for every item with the given userId, handling pagination."""
    items: List[Dict] = []
    response = table.query(KeyConditionExpression=Key(pk_name).eq(user_id))
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.query(
            KeyConditionExpression=Key(pk_name).eq(user_id),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return items


def scan_user(env: str, user_id: str) -> Dict[str, List[Dict]]:
    """Walk every table and return a {table_suffix: [items]} map.

    Tables that don't exist in the target env are reported and skipped.
    """
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    found: Dict[str, List[Dict]] = {}
    for suffix, pk, _sk, _source in TABLES:
        full = table_name(env, suffix)
        table = dynamodb.Table(full)
        try:
            items = query_all_items(table, pk, user_id)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                print(f"  {full:50s}  (table does not exist — skipped)")
                continue
            raise
        found[suffix] = items
        marker = "" if items else "  (empty)"
        print(f"  {full:50s}  {len(items):>5} item(s){marker}")
    return found


def delete_items(env: str, found: Dict[str, List[Dict]]) -> int:
    """Delete every item in `found`. Returns total deleted count."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    total = 0
    for suffix, pk, sk, _source in TABLES:
        items = found.get(suffix, [])
        if not items:
            continue
        full = table_name(env, suffix)
        table = dynamodb.Table(full)
        with table.batch_writer() as batch:
            for item in items:
                key = {pk: item[pk]}
                if sk is not None:
                    key[sk] = item[sk]
                batch.delete_item(Key=key)
        total += len(items)
        print(f"  {full:50s}  deleted {len(items)}")
    return total


def confirm_production(user_id: str) -> bool:
    """Two-step typed confirmation for production. Returns True if confirmed."""
    print()
    print("=" * 72)
    print(" PRODUCTION DELETE — this is destructive and irreversible.")
    print("=" * 72)
    print()
    print(f" Target user: {user_id}")
    print()
    print(" Step 1 of 2: re-type the user ID exactly to confirm.")
    typed_id = input("   user-id> ").strip()
    if typed_id != user_id:
        print("   user ID did not match. Aborted.")
        return False
    print()
    print(" Step 2 of 2: type the phrase  DELETE PRODUCTION USER  to proceed.")
    phrase = input("   phrase>  ").strip()
    if phrase != "DELETE PRODUCTION USER":
        print("   phrase did not match. Aborted.")
        return False
    return True


def confirm_staging(user_id: str) -> bool:
    print()
    print(f" Delete user {user_id} from STAGING? [y/N]")
    return input(" > ").strip().lower() == "y"


def main():
    parser = argparse.ArgumentParser(
        description="Delete all DynamoDB data for a user. Destructive, irreversible.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["staging", "production"],
        help="Target environment. Required — no default.",
    )
    parser.add_argument(
        "--user-id",
        required=True,
        help="User UUID to delete. Required — no default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what WOULD be deleted, then exit without deleting.",
    )
    args = parser.parse_args()

    if not UUID_RE.match(args.user_id):
        print(f"ERROR: --user-id is not a valid UUID: {args.user_id!r}")
        sys.exit(2)

    print()
    print(f"Scanning {args.env} for user {args.user_id} ...")
    print()
    found = scan_user(args.env, args.user_id)

    total_found = sum(len(v) for v in found.values())
    print()
    print(f"Total items found across {len(found)} table(s): {total_found}")

    if total_found == 0:
        print("Nothing to delete. Exiting.")
        sys.exit(0)

    if args.dry_run:
        print()
        print("--dry-run was set. No deletions performed.")
        sys.exit(0)

    if args.env == "production":
        ok = confirm_production(args.user_id)
    else:
        ok = confirm_staging(args.user_id)
    if not ok:
        print("Aborted. Nothing deleted.")
        sys.exit(1)

    print()
    print(f"Deleting from {args.env} ...")
    print()
    total_deleted = delete_items(args.env, found)
    print()
    print(f"Done. Deleted {total_deleted} item(s) across {len(found)} table(s).")


if __name__ == "__main__":
    main()
