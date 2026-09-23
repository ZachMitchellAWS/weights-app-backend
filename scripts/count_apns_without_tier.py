#!/usr/bin/env python3
"""
Count users who registered for push but have NOT unlocked their starting strength tier.
READ-ONLY.

That group is the reachable-but-not-activated audience: push works, so a notification will
land, and they have not yet logged the five fundamental lifts that unlock the tier.

Both facts live on the user-properties row:
  apnsDeviceToken             present  -> push token registered
  hasMetStrengthTierConditions == True -> starting strength tier unlocked

Shares its exclusion list with `plot_user_lift_timelines.py` BY IMPORT, so this count covers
the same population as the other reports.

A user with NO user-properties row at all is counted as "no push, no tier" and also reported
separately -- that is an install that never synced, which is a different thing from a user who
declined the push prompt.

Usage
-----
    # from WeightApp-backend/
    python scripts/count_apns_without_tier.py
    python scripts/count_apns_without_tier.py --env staging
    python scripts/count_apns_without_tier.py --since 2026-08-01T00:00:00.000
    python scripts/count_apns_without_tier.py --list      # print the matching userIds
"""

import argparse
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.plot_user_lift_timelines import (  # noqa: E402
    DEFAULT_EXCLUSIONS,
    DEFAULT_NAME_EXCLUSIONS,
    REGION,
    parse_dt,
    scan_user_properties,
    scan_users,
)


def pct(n, d):
    """`n (xx.x%)`, or `n (n/a)` with no denominator — 0/0 is not a zero rate."""
    return f"{n} (n/a)" if d == 0 else f"{n} ({100.0 * n / d:.1f}%)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="production", choices=["production", "staging"])
    parser.add_argument("--since", default=None,
                        help="Only users whose createdDatetime >= this ISO-8601 instant, "
                             "e.g. 2026-08-01T00:00:00.000")
    parser.add_argument("--exclude", nargs="*", default=DEFAULT_EXCLUSIONS,
                        help="Email substrings to exclude (case-insensitive).")
    parser.add_argument("--exclude-name", nargs="*", default=DEFAULT_NAME_EXCLUSIONS,
                        help="Full-name substrings to exclude (case-insensitive).")
    parser.add_argument("--list", action="store_true",
                        help="Print the userIds of the push-but-no-tier group.")
    args = parser.parse_args()

    since = None
    if args.since:
        since = parse_dt(args.since)
        if since is None:
            print(f"Could not parse --since {args.since!r}; expected ISO-8601.", file=sys.stderr)
            return 2

    try:
        dynamodb = boto3.Session(region_name=REGION).resource("dynamodb")
        users = scan_users(dynamodb, args.env, args.exclude, args.exclude_name)
        props = scan_user_properties(dynamodb, args.env)
    except NoCredentialsError:
        print("No AWS credentials found.", file=sys.stderr)
        return 1
    except ClientError as e:
        print(f"DynamoDB error: {e}", file=sys.stderr)
        return 1

    if since is not None:
        users = [u for u in users if u["created"] and u["created"] >= since]

    # A missing user-properties row is not an error: it means the client never synced. Treat
    # it as neither push-registered nor tier-unlocked, and count it separately below.
    target = []
    buckets = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    no_row = 0
    for u in users:
        p = props.get(u["userId"])
        if p is None:
            no_row += 1
            p = {"apns": False, "tier": False}
        key = (p["apns"], p["tier"])
        buckets[key] += 1
        if p["apns"] and not p["tier"]:
            target.append(u)

    total = len(users)
    scope = f"env={args.env}  users={total}"
    if since:
        scope += f"  since={since:%Y-%m-%d %H:%M}"
    print(f"\nPush registered without starting strength tier — {scope}\n")

    print(f"  PUSH, NO TIER      {pct(len(target), total)}   <- the answer")
    print(f"  push + tier        {pct(buckets[(True, True)], total)}")
    print(f"  no push, no tier   {pct(buckets[(False, False)], total)}")
    print(f"  no push + tier     {pct(buckets[(False, True)], total)}")
    print()
    print(f"  push registered    {pct(buckets[(True, True)] + buckets[(True, False)], total)}")
    print(f"  tier unlocked      {pct(buckets[(True, True)] + buckets[(False, True)], total)}")
    print(f"  no user-properties row at all: {no_row}  (counted above as no push, no tier)")
    print()

    if args.list:
        print("userIds — push registered, tier not unlocked:")
        for u in sorted(target, key=lambda x: x["userId"]):
            created = f"{u['created']:%Y-%m-%d}" if u["created"] else "—"
            print(f"  {u['userId']}  signed up {created}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
