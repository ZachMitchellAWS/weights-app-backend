#!/usr/bin/env python3
"""
Schedule `unlock-strength-tier-nudge` notification tasks. REVIEW FIRST, WRITE ONLY ON --commit.

Selects users who signed up a while ago, never finished onboarding, and can actually be
reached, then queues one nudge each for 5:00pm local on the coming Monday.

THE DEFAULT RUN WRITES NOTHING. It prints the full intended schedule — who, in which timezone,
at what local and UTC time, and how many decimal days out — and stops. `--commit` is what
writes. This sends push notifications to real people, so the safe mode has to be the one you
get by forgetting a flag.

Selection (all must hold)
-------------------------
  * not on the shared exclusion list (imported from plot_user_lift_timelines)
  * signed up >= --days-since-signup ago (default 7) — the client already schedules its own
    onboarding notification, and this floor keeps the two from landing on top of each other
  * `hasMetStrengthTierConditions` is not True
  * has a usable APNs token: a row in `apns-tokens` that is neither `invalid` nor `loggedOut`,
    or a legacy `apnsDeviceToken` on user-properties
  * no active non-sandbox entitlement grant (free users only)

De-duplication
--------------
Skipped if the user already has an unsent task of this type, or a `notification-log` row for it
within --dedup-days (default 30) — ANY outcome, including `skipped_precondition`, because a
cancelled send still consumed its turn.

Usage
-----
    # from WeightApp-backend/
    python scripts/schedule_tier_nudges.py                      # review production, write nothing
    python scripts/schedule_tier_nudges.py --commit             # actually schedule
    python scripts/schedule_tier_nudges.py --env staging --user-id <uuid> --commit
    python scripts/schedule_tier_nudges.py --csv /tmp/plan.csv
"""

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, NoCredentialsError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# The notification utils are Lambda code, not a package rooted at the repo — add its own root so
# `utils.tasks` resolves the same way it does inside the function.
sys.path.insert(0, str(REPO_ROOT / "services" / "notifications" / "lambda"))

from scripts.plot_user_lift_timelines import (  # noqa: E402
    REGION,
    parse_dt,
    scan_users,
    table_name,
)
# Imported, never reimplemented. The 15-minute binning and the shard assignment must match what
# the Lambda queries byte for byte — a second copy of that arithmetic is exactly how a task
# becomes invisible to the sweeper and sits until its TTL.
from utils.tasks import bin_for, build_task  # noqa: E402

NOTIFICATION_TYPE = "unlock-strength-tier-nudge"

# Monday 17:00 local. Monday=0 in `weekday()`.
#
# A DELIBERATE PRODUCT CHOICE, NOT A MEASURED ONE — and it runs against the measurement, so the
# measurement is kept here rather than quietly deleted. Production behaviour on 2026-09-18
# (after excluding the owner, the App Store reviewer and the plus-addressed test accounts, which
# together were ~70% of all logged sets and had made the raw histogram a picture of one person's
# routine) said:
#
#   day      Wed led workout-days (27), distinct users (19) AND volume (204) simultaneously.
#            Monday was not the leader on any of the three. Saturday was the only clear
#            negative at 9.
#   hour     08:00-12:00 carried ~45% of sets; 11:00 had the widest participation (12 users).
#            The 17:00-18:00 stretch was among the weakest of the active day.
#
# Why override it anyway: that data describes people who ALREADY train, and the nudge targets
# people who have never logged their five lifts — they are absent from it by definition. It
# says when existing users happen to lift, not when a stalled user is most willing to start.
# Monday evening bets on start-of-week intent instead, which the data cannot speak to either way.
#
# So: treat BOTH the old timing and this one as guesses. This is the thing to A/B once
# `notification-log` holds enough sends to compare open and unlock rates directly.
TARGET_WEEKDAY = 0
TARGET_LOCAL_TIME = time(17, 0)

# Derived once, used everywhere a human-readable target is printed — help text, review banner.
# The old code hardcoded "Thursday 17:30" in four places and all four were still saying it two
# retimes later. Deriving it is the only way that stops happening.
TARGET_DAY_NAME = ["Monday", "Tuesday", "Wednesday", "Thursday",
                   "Friday", "Saturday", "Sunday"][TARGET_WEEKDAY]
TARGET_LABEL = f"{TARGET_DAY_NAME} {TARGET_LOCAL_TIME:%H:%M} local"

# Used when a user has no `timezone` and no `utcOffsetSeconds`. A GUESS, and 5:00pm local is the
# entire point of the schedule — rows using it are marked in the review output so the assumption
# is visible at approval time rather than buried here.
DEFAULT_TIMEZONE = "America/New_York"

# Per-environment defaults. Staging exists to exercise the plumbing, not to model the product:
# there is one test account, it signed up whenever it signed up, and waiting until Monday
# evening to find out whether a task fires is not a test loop. So staging drops the signup
# floor, drops de-duplication, and schedules for the next sweep instead of the weekly target.
#
# Production keeps all three. Every one of them is still overridable by flag in both
# environments — these are defaults, not behaviour changes.
ENV_DEFAULTS = {
    "production": {"days_since_signup": 7, "dedup_days": 30, "dedup": True, "asap": False},
    "staging":    {"days_since_signup": 0, "dedup_days": 0,  "dedup": False, "asap": True},
}

# THIS SCRIPT HAS ITS OWN EXCLUSION LIST, deliberately not the one the reporting scripts use.
#
# `DEFAULT_EXCLUSIONS` in plot_user_lift_timelines excludes on the bare word "zach", which also
# catches real users whose address merely contains it, plus internal accounts that are real
# people using the app. Those scripts exclude to keep analysis clean; this one decides who
# receives a push, and silently withholding a nudge someone qualifies for is a different
# question with a different answer.
#
# What this needs to drop is narrower: plus-addressed TEST accounts, which are always
# `zach+something@...`. The `+` is what makes it precise — it matches the alias convention and
# nothing else.
#
# APPLIED ONLY TO BULK SELECTION. `--user-id` skips the selection loop entirely, so a targeted
# run reaches a test account by design; that is how these get tested in a controlled way.
NUDGE_EXCLUSIONS = ["zach+"]


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def next_target(now_utc: datetime, tz_name: str) -> datetime:
    """The next `TARGET_LABEL` in `tz_name` that is strictly in the future, as UTC.

    Built as a NAIVE local datetime with the zone attached to that date, rather than by shifting
    an already-aware value: a target on the far side of a DST boundary would otherwise inherit
    today's offset and land an hour off. 17:00 never falls inside a US transition window (those
    run at 02:00 local), so there is no ambiguous or imaginary time to disambiguate.

    "Strictly in the future" is what makes "the closest Monday" unambiguous — Monday 4pm
    schedules today, Monday 6pm schedules a week out.
    """
    zone = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(zone)
    days_ahead = (TARGET_WEEKDAY - local_now.weekday()) % 7
    naive = datetime.combine(local_now.date() + timedelta(days=days_ahead), TARGET_LOCAL_TIME)
    aware = naive.replace(tzinfo=zone)
    if aware <= local_now:
        aware = (naive + timedelta(days=7)).replace(tzinfo=zone)
    return aware.astimezone(timezone.utc)


def asap_target(now_utc: datetime) -> datetime:
    """The soonest a task can possibly be delivered: right now.

    `bin_for` floors to the current 15-minute bin, which is already ripe by the sweeper's
    `< next_bin` test, so the task goes out on the very next tick rather than waiting a full
    bin. There is no way to be sooner than that without bypassing the queue entirely, which is
    what the SEND_NOW pathway is for.
    """
    return now_utc


def resolve_timezone(props: dict, default_tz: str) -> tuple[str, bool]:
    """-> (tz_name, was_assumed). Falls back to `default_tz` for a user with none recorded."""
    tz = props.get("timezone")
    if isinstance(tz, str) and tz.strip():
        try:
            ZoneInfo(tz.strip())
            return tz.strip(), False
        except (ZoneInfoNotFoundError, ValueError):
            pass  # recorded but unusable — treat as missing rather than crashing the run
    return default_tz, True


# --------------------------------------------------------------------------- #
# DynamoDB (all reads except the --commit write)
# --------------------------------------------------------------------------- #
def _scan(dynamodb, name: str, optional: bool = False) -> list[dict]:
    try:
        table = dynamodb.Table(name)
        items, kwargs = [], {}
        while True:
            resp = table.scan(**kwargs)
            items += resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return items
    except ClientError as e:
        if optional and e.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"  note: {name} does not exist — treating as empty")
            return []
        raise


def active_paid_users(dynamodb, env: str, now: datetime) -> set[str]:
    """Users with a live non-sandbox entitlement.

    `!= "Sandbox"` rather than `== "Production"`: the oldest real grants predate the attribute
    entirely, and an equality test would count them as free.
    """
    paid = set()
    for g in _scan(dynamodb, table_name(env, "entitlement-grants")):
        if g.get("transactionEnvironment") == "Sandbox":
            continue
        end = parse_dt(g.get("endUtc"))
        if end and end > now:
            paid.add(g["userId"])
    return paid


def usable_token_owners(dynamodb, env: str, props_by_user: dict) -> set[str]:
    """Everyone the sender could actually reach right now.

    Mirrors `services/notifications/lambda/utils/tokens.py` deliberately, fallback included.
    Selecting a user the sender would then find unreachable is the one mismatch that would make
    this report lie about what it is going to do.
    """
    owners = set()
    for row in _scan(dynamodb, table_name(env, "apns-tokens"), optional=True):
        if row.get("invalid") is not True and row.get("loggedOut") is not True:
            owners.add(row["userId"])
    for user_id, props in props_by_user.items():
        if props.get("apnsDeviceToken"):
            owners.add(user_id)
    return owners


def pending_task_owners(dynamodb, env: str) -> set[str]:
    """Users with an unsent task of this type already queued."""
    return {
        t["userId"] for t in _scan(dynamodb, table_name(env, "notification-tasks"), optional=True)
        if t.get("notificationType") == NOTIFICATION_TYPE
    }


def recently_notified(dynamodb, env: str, user_ids: set[str], since: datetime) -> dict[str, str]:
    """-> {userId: last sentAt} for anyone sent this type since `since`.

    Queried per candidate rather than scanned: the log grows without bound and the candidate
    list is small. Counts EVERY outcome — a nudge cancelled by its precondition still used up
    that user's turn, and re-queueing it would just cancel again.
    """
    try:
        table = dynamodb.Table(table_name(env, "notification-log"))
    except ClientError:
        return {}
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for user_id in user_ids:
        try:
            resp = table.query(
                KeyConditionExpression=(
                    Key("userId").eq(user_id) & Key("sentAtTaskId").gte(cutoff)
                ),
                ScanIndexForward=False,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return {}
            raise
        for row in resp.get("Items", []):
            if row.get("notificationType") == NOTIFICATION_TYPE:
                out[user_id] = row["sentAtTaskId"].split("#")[0]
                break
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_plan(rows: list[dict], rejected: Counter, skipped: list[dict],
               env: str, now: datetime, committed: bool, mode: str = "") -> None:
    print(f"\nTier-unlock nudges — env={env}  now={now:%Y-%m-%d %H:%M}Z")
    if mode:
        print(f"  {mode}")
    print()

    if rows:
        print(f"  {'email':34s} {'userId':10s} {'timezone':24s} "
              f"{'local send':22s} {'utc send':18s} {'days':>6s}")
        for r in sorted(rows, key=lambda x: x["days_out"]):
            tz_label = r["timezone"] + (" *" if r["assumed_tz"] else "")
            email = r.get("email") or "—"
            email = email if len(email) <= 34 else email[:33] + "…"
            print(f"  {email:34s} {r['userId'][:8]}…  {tz_label:24s} "
                  f"{r['local']:%a %m-%d %H:%M}      {r['utc']:%Y-%m-%d %H:%M}Z "
                  f"{r['days_out']:>6.2f}")
        if any(r["assumed_tz"] for r in rows):
            print(f"\n  * timezone ASSUMED ({rows[0]['default_tz']}) — none recorded for this user.")
            print(f"    {TARGET_LOCAL_TIME:%H:%M} local is the point of the schedule; "
                  f"check these rows specifically.")
    else:
        print("  No users qualify.")

    if rejected:
        print("\n  filtered out:")
        for reason, count in rejected.most_common():
            print(f"    {reason:34s} {count}")

    if skipped:
        print(f"\n  skipped as already handled ({len(skipped)}):")
        for s in skipped:
            print(f"    {(s.get('email') or s['userId'][:8] + '…'):36s} {s['reason']}")

    print()
    if committed:
        print(f"  COMMITTED — {len(rows)} task(s) written.")
    else:
        print(f"  {len(rows)} task(s) would be written. Nothing has been scheduled.")
        print("  Re-run with --commit to schedule.")
    print()


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["userId", "email", "timezone", "timezoneAssumed", "localSend", "utcSend",
                    "dueBinUtc", "daysOut"])
        for r in sorted(rows, key=lambda x: x["days_out"]):
            w.writerow([r["userId"], r.get("email", ""), r["timezone"], r["assumed_tz"],
                        r["local"].strftime("%Y-%m-%d %H:%M %Z"),
                        r["utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                        r["bin"], f"{r['days_out']:.2f}"])
    print(f"  Wrote {path}\n")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="production", choices=["production", "staging"])
    parser.add_argument("--commit", action="store_true",
                        help="Actually write the tasks. Without this, nothing is written.")
    parser.add_argument("--user-id", default=None,
                        help="Schedule this one user, bypassing selection (keeps de-dup). "
                             "Staging's cohort is normally empty, so this is the test path.")
    parser.add_argument("--days-since-signup", type=int, default=None,
                        help="Minimum account age. Default: 7 on production, 0 on staging.")
    parser.add_argument("--dedup-days", type=int, default=None,
                        help="Skip users sent this type within N days. "
                             "Default: 30 on production, 0 on staging.")
    parser.add_argument("--dedup", dest="dedup", action="store_true", default=None,
                        help="Force de-duplication on (default on production).")
    parser.add_argument("--no-dedup", dest="dedup", action="store_false",
                        help="Skip both de-dup checks (default on staging).")
    parser.add_argument("--asap", dest="asap", action="store_true", default=None,
                        help=f"Schedule for the next sweep instead of {TARGET_LABEL} "
                             f"(default on staging).")
    # Named for the behaviour, not the weekday. This flag was --thursday, then the target moved
    # to Wednesday, then to Monday; the name outlived two of its own meanings.
    parser.add_argument("--scheduled", "--weekly", dest="asap", action="store_false",
                        help=f"Force {TARGET_LABEL} (default on production).")
    parser.add_argument("--default-timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--exclude", nargs="*", default=None,
                        help=f"Email substrings to exclude from BULK selection. "
                             f"Default: {NUDGE_EXCLUSIONS}. Pass with no values to disable. "
                             f"Never applies to --user-id.")
    parser.add_argument("--exclude-name", nargs="*", default=None,
                        help="Full-name substrings to exclude. None by default.")
    parser.add_argument("--bypass-precondition", action="store_true",
                        help="Set bypassPrecondition on the written tasks, so they deliver even "
                             "if the user no longer qualifies. Requires --user-id.")
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args()

    # The two flags are inseparable on purpose. A BULK run with the bypass set would nudge
    # every user who has already unlocked their tier — precisely the outcome the precondition
    # exists to prevent — so the dangerous combination should not be typeable by accident.
    if args.bypass_precondition and not args.user_id:
        print("--bypass-precondition requires --user-id: it is a single-account test "
              "affordance, not a bulk mode.", file=sys.stderr)
        return 2

    try:
        ZoneInfo(args.default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Unknown --default-timezone {args.default_timezone!r}", file=sys.stderr)
        return 2

    defaults = ENV_DEFAULTS[args.env]
    for key in ("days_since_signup", "dedup_days", "dedup", "asap"):
        if getattr(args, key) is None:
            setattr(args, key, defaults[key])
    args.exclude = NUDGE_EXCLUSIONS if args.exclude is None else args.exclude
    args.exclude_name = args.exclude_name or []

    now = datetime.now(timezone.utc)
    naive_now = now.replace(tzinfo=None)

    try:
        dynamodb = boto3.Session(region_name=REGION).resource("dynamodb")
        # Scanned unfiltered on purpose: the exclusion is applied in the selection loop below
        # so it appears in the "filtered out" counts. A filter that removes people before the
        # funnel is invisible, which is exactly what cost an hour of debugging on staging.
        users = scan_users(dynamodb, args.env, [], args.exclude_name)
        props_by_user = {p["userId"]: p
                         for p in _scan(dynamodb, table_name(args.env, "user-properties"))}
        token_owners = usable_token_owners(dynamodb, args.env, props_by_user)
        paid = active_paid_users(dynamodb, args.env, naive_now)
        pending = pending_task_owners(dynamodb, args.env)
    except NoCredentialsError:
        print("No AWS credentials found.", file=sys.stderr)
        return 1
    except ClientError as e:
        print(f"DynamoDB error: {e}", file=sys.stderr)
        return 1

    signup_cutoff = naive_now - timedelta(days=args.days_since_signup)
    rejected = Counter()
    candidates = []

    if args.user_id:
        match = next((u for u in users if u["userId"] == args.user_id), None)
        if match is None:
            match = {"userId": args.user_id, "created": None, "email": "", "name": ""}
            print(f"  note: {args.user_id} not in the user scan — scheduling anyway (--user-id)")
        candidates = [match]
    else:
        for u in users:
            email = (u.get("email") or "").lower()
            if any(x in email for x in args.exclude):
                rejected[f"test account ({'/'.join(args.exclude)})"] += 1
                continue
            if not (u["created"] and u["created"] <= signup_cutoff):
                rejected["signed up too recently"] += 1
                continue
            p = props_by_user.get(u["userId"], {})
            if p.get("hasMetStrengthTierConditions") is True:
                rejected["tier already unlocked"] += 1
                continue
            if u["userId"] not in token_owners:
                rejected["no usable APNs token"] += 1
                continue
            if u["userId"] in paid:
                rejected["premium"] += 1
                continue
            candidates.append(u)

    # De-dup after selection so the funnel counts stay meaningful.
    if args.dedup:
        dedup_since = naive_now - timedelta(days=args.dedup_days)
        notified = recently_notified(dynamodb, args.env,
                                     {c["userId"] for c in candidates}, dedup_since)
    else:
        # Both checks off together. Half-disabled de-dup would be worse than none: it reads as
        # protection while letting duplicates through the other path.
        notified, pending = {}, set()

    rows, skipped = [], []
    for c in candidates:
        if c["userId"] in pending:
            skipped.append({"userId": c["userId"], "email": c.get("email", ""),
                            "reason": "task already queued"})
            continue
        if c["userId"] in notified:
            skipped.append({"userId": c["userId"], "email": c.get("email", ""),
                            "reason": f"sent {notified[c['userId']][:10]} "
                                      f"(within {args.dedup_days}d)"})
            continue
        tz_name, assumed = resolve_timezone(props_by_user.get(c["userId"], {}),
                                            args.default_timezone)
        target_utc = asap_target(now) if args.asap else next_target(now, tz_name)
        rows.append({
            "userId": c["userId"],
            "email": c.get("email", ""),
            "timezone": tz_name,
            "assumed_tz": assumed,
            "default_tz": args.default_timezone,
            "local": target_utc.astimezone(ZoneInfo(tz_name)),
            "utc": target_utc,
            "bin": bin_for(target_utc),
            "days_out": (target_utc - now).total_seconds() / 86400.0,
        })

    if args.commit and rows:
        table = dynamodb.Table(table_name(args.env, "notification-tasks"))
        for r in rows:
            item = build_task(r["userId"], NOTIFICATION_TYPE,
                              r["utc"].replace(tzinfo=None))
            if args.bypass_precondition:
                item["bypassPrecondition"] = True
            table.put_item(Item=item)
            r["bin"] = item["dueAtTaskId"].split("#")[0]

    # Derived from the constants, never hardcoded: a banner that disagrees with what the
    # script actually does is worse than no banner.
    mode = (f"schedule={'ASAP (next sweep)' if args.asap else TARGET_LABEL}  "
            f"min account age={args.days_since_signup}d  "
            f"de-dup={'on, ' + str(args.dedup_days) + 'd' if args.dedup else 'OFF'}  "
            f"exclusions={','.join(args.exclude) if args.exclude else 'none'}"
            + ("  bypass=ON" if args.bypass_precondition else ""))
    print_plan(rows, rejected, skipped, args.env, now,
               committed=bool(args.commit and rows), mode=mode)
    if args.csv:
        write_csv(rows, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
