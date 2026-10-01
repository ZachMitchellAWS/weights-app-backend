#!/usr/bin/env python3
"""
Subscription status + conversion report. READ-ONLY.

Lists who is currently on a free trial (with days remaining) and who is premium, then reports
free -> trial and trial -> paid conversion over the same population.

Shares its exclusion list with `plot_user_lift_timelines.py` BY IMPORT, not by copy, so both
reports always describe the same users. Editing the defaults there moves this report too.

Two things about the data are worth knowing before reading the output
--------------------------------------------------------------------
1. SANDBOX GRANTS DOMINATE THE TABLE. At time of writing, 80 of 91 production grants are
   `transactionEnvironment: "Sandbox"` -- StoreKit test purchases from development devices,
   with accelerated 1-day renewals. They are excluded by default. `--include-sandbox` puts
   them back when you are checking a purchase you just made on a device.

   The filter is `!= "Sandbox"` rather than `== "Production"` on purpose: the oldest real
   grants predate the attribute and carry no `transactionEnvironment` at all.

2. TRIAL STATUS IS INFERRED FROM GRANT DURATION, NOT STORED. Apple reports a free trial via
   `offerType` / `offerDiscountType`, and `_create_entitlement_grant` in the entitlements
   Lambda does not persist either. (iOS computes `transaction.offer?.paymentMode ==
   .freeTrial` in PurchaseService.swift but sends it only to Amplitude.) So a grant spanning
   <= TRIAL_MAX_DAYS is read as a trial and one spanning >= PAID_MIN_DAYS as paid. Real
   non-sandbox spans cluster hard at 7.00 and 365.00 days, so this separates cleanly today --
   but if the trial length is ever changed in App Store Connect, TRIAL_MAX_DAYS must move
   with it. Every row prints the raw spans it was judged on so a misread is visible.

Usage
-----
    # from WeightApp-backend/
    python scripts/report_subscription_status.py
    python scripts/report_subscription_status.py --env staging
    python scripts/report_subscription_status.py --since 2026-09-06T02:33:00.000
    python scripts/report_subscription_status.py --include-sandbox
    python scripts/report_subscription_status.py --csv /tmp/subs.csv

`--since` filters on the user's `createdDatetime` and is GLOBAL: it defines the population
for the listing and for every conversion percentage.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# The report is meant to be run as `python scripts/report_subscription_status.py` from the
# repo root, which puts `scripts/` on sys.path but not its parent -- so the package import
# below needs the parent added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.plot_user_lift_timelines import (  # noqa: E402
    REGION,
    parse_dt,
    scan_users,
    table_name,
)

# THIS REPORT HAS ITS OWN EXCLUSION LIST, deliberately not the shared one.
#
# `DEFAULT_EXCLUSIONS` in plot_user_lift_timelines matches the bare word "zach", which is fine
# for filtering noise out of an activity chart and wrong here: it silently dropped
# `zacharydwight.mayfield@gmail.com`, a paying customer whose only crime is being called
# Zachary. A revenue report that quietly omits real subscribers is worse than one with a bit
# of noise in it.
#
# What genuinely does not belong in subscription statistics is an account that cannot be a
# CUSTOMER: the owner's own accounts and the App Store reviewer account, which holds a comped
# grant. Everyone else counts, including friends and family — they paid.
#
# Matching is by SUBSTRING, so each entry must be specific enough to hit only its intended
# account: `zach+` matches the plus-addressing convention, `zachmitchell002` is a full local
# part. A bare `zach` would catch real customers — it already did, which is why this list
# exists separately from the shared one.
TEST_ACCOUNT_SUBSTRINGS = ["zach+", "zachmitchell002"]
NON_CUSTOMER_EMAILS = {"review@liftthebull.io"}

# Duration bounds for reading a grant's purpose. Deliberately loose rather than exact-match on
# 7 and 365: a tolerant band survives a retuned trial length or a proration, and anything that
# lands between the two bounds is reported as UNKNOWN rather than guessed at.
# Earliest account seen carrying `hasCompletedOnboarding`. Anything older predates the flag
# and reads as incomplete regardless of what the user actually did.
ONBOARDING_FLAG_SHIPPED = datetime(2026, 8, 1)

TRIAL_MAX_DAYS = 14
PAID_MIN_DAYS = 28

# Display order. ON TRIAL and PREMIUM are the answer to the question; the rest is context.
STATUS_ORDER = ["ON TRIAL", "PREMIUM", "TRIAL LAPSED", "CHURNED", "UNKNOWN"]

# Grants are stored in UTC, which is right for storage and useless for answering "is that
# tonight or tomorrow morning?". Displayed in a real timezone instead, as a whole datetime —
# for a trial ending in hours, the date alone is the part that matters least.
REPORT_TIMEZONE = "America/Los_Angeles"


def _local(dt, tz_name):
    """Naive-UTC -> aware local.

    `parse_dt` normalises everything to naive UTC on the way in, so the tzinfo has to be
    reattached before converting — `astimezone` on a naive value assumes system local and
    would apply the offset twice.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


# --------------------------------------------------------------------------- #
# DynamoDB (read-only)
# --------------------------------------------------------------------------- #
def scan_table(dynamodb, name: str, optional: bool = False) -> list[dict]:
    """Full scan, tolerating a table that does not exist yet."""
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
            return []
        raise


def scan_grants(dynamodb, env: str, include_sandbox: bool = False):
    """Scan entitlement-grants -> {userId: [grant, ...]} sorted oldest-first. Read-only."""
    table = dynamodb.Table(table_name(env, "entitlement-grants"))
    grants = defaultdict(list)
    scan_kwargs = {}
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            if not include_sandbox and item.get("transactionEnvironment") == "Sandbox":
                continue
            grants[item["userId"]].append(item)
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    for gs in grants.values():
        gs.sort(key=lambda g: str(g.get("startUtc", "")))
    return dict(grants)


def auto_renew_state(dynamodb, env: str) -> dict[str, str]:
    """-> {originalTransactionId: "on"|"off"} from Apple's renewal-status notifications.

    WHY THIS TABLE AND NOT `entitlement-grants`: a cancelled trial and a healthy one are
    BYTE-IDENTICAL as grants — both are a live 7-day row. Apple does not revoke access when
    someone turns off auto-renew; it just stops the renewal. The only trace is a
    `DID_CHANGE_RENEWAL_STATUS` notification, so a report built on grants alone reads every
    doomed trial as if it were still converting.

    Keyed on `originalTransactionId` rather than userId: that is the subscription's identity,
    and it is what lets a user who resubscribes later not inherit the old cancellation.

    Last event wins — AUTO_RENEW_DISABLED followed by AUTO_RENEW_ENABLED means they changed
    their mind, and the order is the answer.
    """
    events = []
    for e in scan_table(dynamodb, table_name(env, "subscription-events"), optional=True):
        if e.get("notificationType") != "DID_CHANGE_RENEWAL_STATUS":
            continue
        txn = e.get("originalTransactionId")
        if txn:
            events.append((str(e.get("eventTimestamp", "")), txn, e.get("subtype", "")))
    state = {}
    for _, txn, subtype in sorted(events):
        if subtype == "AUTO_RENEW_DISABLED":
            state[txn] = "off"
        elif subtype == "AUTO_RENEW_ENABLED":
            state[txn] = "on"
    return state


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def span_days(grant):
    """How many days a grant covers, or None if either endpoint is unparseable.

    `parse_dt` is doing real work here: endUtc values are inconsistently timezone-aware
    across the table, and subtracting a naive from an aware datetime raises TypeError.
    """
    start, end = parse_dt(grant.get("startUtc")), parse_dt(grant.get("endUtc"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 86400.0


def latest_grant(grants):
    """The grant that decides current status: the one running furthest into the future."""
    return max(grants, key=lambda g: str(g.get("endUtc", "")))


def classify(grants, now):
    """-> (status, spans, end_dt, days_remaining, product)."""
    if not grants:
        return "FREE", [], None, None, "", ""

    spans = [s for s in (span_days(g) for g in grants) if s is not None]
    ever_trial = any(s <= TRIAL_MAX_DAYS for s in spans)
    ever_paid = any(s >= PAID_MIN_DAYS for s in spans)

    latest = latest_grant(grants)
    end = parse_dt(latest.get("endUtc"))
    latest_span = span_days(latest)
    product = str(latest.get("productId", ""))
    # "com.weightapp.premium.yearly.3999" -> "yearly"
    short_product = product.split(".")[-2] if product.count(".") >= 2 else product

    active = end is not None and end > now

    if latest_span is None or end is None:
        status = "UNKNOWN"
    elif active and latest_span <= TRIAL_MAX_DAYS:
        status = "ON TRIAL"
    elif active and latest_span >= PAID_MIN_DAYS:
        status = "PREMIUM"
    elif active:
        status = "UNKNOWN"  # a span between the bounds — do not guess which side it is on
    elif ever_paid:
        status = "CHURNED"
    elif ever_trial:
        status = "TRIAL LAPSED"
    else:
        status = "UNKNOWN"

    days_remaining = int((end - now).total_seconds() // 86400) if active else None
    return (status, spans, end, days_remaining, short_product,
            str(latest.get("originalTransactionId", "")))


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
def ratio(numerator, denominator):
    """`n/d (xx.x%)`, or `n/d (n/a)` when there is no denominator.

    A zero denominator is not a zero rate. Printing "0.0%" for 0/0 would report a total
    conversion failure where there is simply nothing settled yet — which is exactly what a
    narrow `--since` window produces.
    """
    if denominator == 0:
        return f"{numerator}/0 (n/a)"
    return f"{numerator}/{denominator} ({100.0 * numerator / denominator:.1f}%)"


def product_of(grant) -> str:
    """`yearly` / `monthly` from the product id, or `?`."""
    pid = str(grant.get("productId", ""))
    return pid.split(".")[-2] if pid.count(".") >= 2 else "?"


def activation_stats(cohort_ids, props_by_user, oldest_created=None):
    """How far users get before money is involved. Both ratios are over the whole cohort.

    `hasCompletedOnboarding` is only ever written TRUE — nothing sets it false — so absent
    means "has not completed", and the full cohort is the right denominator.

    The one wrinkle is history: SCHEMA.md records the flag as never backfilled, so accounts
    predating it also read as absent and are indistinguishable from a genuine drop-off. That
    only distorts an ALL-TIME run; scoped with `--since` to any window after the flag shipped,
    every account carries it and the number is exact. The caveat prints only when the cohort
    actually reaches back that far.
    """
    onboarded = sum(1 for uid in cohort_ids
                    if (props_by_user.get(uid) or {}).get("hasCompletedOnboarding") is True)
    tier = sum(1 for uid in cohort_ids
               if (props_by_user.get(uid) or {}).get("hasMetStrengthTierConditions") is True)
    return {
        "total": len(cohort_ids),
        "onboarded": onboarded,
        "tier_unlocked": tier,
        "oldest_created": oldest_created,
    }


def conversion_stats(cohort_ids, grants_by_user, now, renewal=None):
    """Conversion measured against the WHOLE POPULATION, per product.

    EVERY denominator is the total user count, including the paid rows. Dividing converts by
    trial-starters answers "of people who tried, how many bought"; dividing by everyone answers
    "of people who showed up, how many pay" — which is the question that matters for whether
    the business works, and it keeps all the percentages on one comparable scale.

    Two figures per product:
      confirmed  users who have ever held a PAID grant of it. Includes people who later
                 churned — they did convert, and pretending otherwise flatters nothing.
      projected  confirmed PLUS everyone currently mid-trial who has not cancelled, i.e. the
                 ceiling if every undecided trial converts. The gap between the two is the
                 amount of the number that is still hope rather than fact.

    MONTHLY HAS NO TRIAL (`trialEligibleProduct = yearlyProductId` in SubscriptionConfig.swift),
    so its projected always equals its confirmed. That is reported rather than hidden: an
    always-equal pair is information about the product, not a bug in the report.
    """
    renewal = renewal or {}
    total = len(cohort_ids)
    trial_starters = set()
    paid = {"yearly": set(), "monthly": set()}
    pending = {"yearly": set(), "monthly": set()}
    cancelled = {"yearly": set(), "monthly": set()}

    for uid in cohort_ids:
        grants = grants_by_user.get(uid) or []
        if not grants:
            continue
        for g in grants:
            span = span_days(g)
            if span is None:
                continue
            prod = product_of(g)
            if prod not in paid:
                continue
            if span >= PAID_MIN_DAYS:
                paid[prod].add(uid)
            elif span <= TRIAL_MAX_DAYS:
                trial_starters.add(uid)

        latest = latest_grant(grants)
        span, end = span_days(latest), parse_dt(latest.get("endUtc"))
        prod = product_of(latest)
        # Still running, still a trial, and they have not switched auto-renew off. A cancelled
        # trial is already decided, so it is not pending — counting it would make `projected`
        # a projection of something we know will not happen.
        auto_renew = renewal.get(str(latest.get("originalTransactionId", "")))
        if (prod in pending and span is not None and span <= TRIAL_MAX_DAYS
                and end and end > now and auto_renew != "off"):
            pending[prod].add(uid)

        # Cancelled = they turned auto-renew OFF, whether mid-trial or as a paying subscriber.
        # Counted on the LATEST grant only: an old cancellation followed by resubscribing is
        # not a current cancellation, and `renewal` is already keyed per subscription so a
        # later purchase carries its own state.
        if prod in cancelled and auto_renew == "off":
            cancelled[prod].add(uid)

    return {
        "total": total,
        "trial_starters": len(trial_starters),
        "products": {
            prod: {
                "confirmed": len(paid[prod]),
                "projected": len(paid[prod] | pending[prod]),
                "pending": len(pending[prod] - paid[prod]),
                "cancelled": len(cancelled[prod]),
                # Of the people who reached this product at all — the churn rate, which is a
                # different question from the population-wide percentage next to it.
                "reached": len(paid[prod] | pending[prod] | cancelled[prod]),
            }
            for prod in ("yearly", "monthly")
        },
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_report(rows, stats, env, since, include_sandbox, grant_count, act=None,
                 tz_name=REPORT_TIMEZONE):
    tz_label = datetime.now(ZoneInfo(tz_name)).strftime("%Z")
    scope = f"env={env}  users={stats['total']}  grants={grant_count}  times={tz_name}"
    if since:
        scope += f"  since={since:%Y-%m-%d %H:%M}"
    if include_sandbox:
        scope += "  [SANDBOX INCLUDED]"
    print(f"\nSubscription status — {scope}\n")

    by_status = defaultdict(list)
    for r in rows:
        by_status[r["status"]].append(r)

    for status in STATUS_ORDER:
        group = by_status.get(status)
        if not group:
            continue
        group.sort(key=lambda r: (r["days_remaining"] is None, r["days_remaining"], r["userId"]))
        print(f"{status}  ({len(group)})")
        print(f"  {'userId':36s} {'remaining':>10s}  {'ends (' + tz_label + ')':22s}  {'product':8s}  "
              f"{'renewal':11s}  {'signed up':10s}  spans")
        for r in group:
            remaining = f"{r['days_remaining']}d" if r["days_remaining"] is not None else "—"
            local_end = _local(r["end"], tz_name)
            ends = f"{local_end:%Y-%m-%d %H:%M %Z}" if local_end else "—"
            signup = f"{r['created']:%Y-%m-%d}" if r["created"] else "—"
            spans = "[" + ", ".join(f"{s:.1f}" for s in r["spans"]) + "]"
            print(f"  {r['userId']:36s} {remaining:>10s}  {ends:22s}  "
                  f"{r['product']:8s}  {r['renewal']:11s}  {signup:10s}  {spans}")
        print()

    free = len(by_status.get("FREE", []))
    print(f"FREE  ({free})  — no non-sandbox entitlement grant\n")

    n = stats["total"]
    if act:
        print(f"Activation  —  of the full population ({n} users)\n")
        print(f"  {'completed onboarding':26s}{ratio(act['onboarded'], n)}")
        print(f"  {'unlocked strength tier':26s}{ratio(act['tier_unlocked'], n)}")
        # `hasCompletedOnboarding` shipped mid-life and was never backfilled, so an all-time
        # run counts pre-feature accounts as drop-offs. Flag it only when the cohort is old
        # enough for that to be happening.
        oldest = act.get("oldest_created")
        if oldest and oldest < ONBOARDING_FLAG_SHIPPED:
            print(f"  {'':26s}note: accounts created before {ONBOARDING_FLAG_SHIPPED:%Y-%m-%d} "
                  f"never had the onboarding flag set, so they read as incomplete.")
            print(f"  {'':26s}      use --since to scope past that for an exact figure.")
        print()

    print(f"Conversion  —  all percentages are of the full population ({n} users)\n")
    print(f"  {'free → trial':26s}{ratio(stats['trial_starters'], n)}")
    print()
    print(f"  {'':26s}{'confirmed':>16s}   {'if pending convert':>18s}   "
          f"{'cancelled':>16s}")
    for prod in ("yearly", "monthly"):
        d = stats["products"][prod]
        note = ""
        if d["pending"] == 0 and prod == "monthly":
            note = "   (no trial — nothing pending)"
        print(f"  {prod + ' → paid':26s}{ratio(d['confirmed'], n):>16s}   "
              f"{ratio(d['projected'], n):>18s}   {ratio(d['cancelled'], n):>16s}{note}")
    print()
    for prod in ("yearly", "monthly"):
        d = stats["products"][prod]
        if d["reached"]:
            print(f"  {prod + ' churn':26s}{ratio(d['cancelled'], d['reached'])}"
                  f"   of the {d['reached']} who took it up")
    pend = sum(stats["products"][p]["pending"] for p in stats["products"])
    if pend:
        print(f"\n  {pend} trial(s) still undecided — the gap between the two columns. "
              f"Cancelled trials are not counted as pending.")
    print()
    print("Trial vs paid is inferred from grant duration (<= "
          f"{TRIAL_MAX_DAYS}d trial, >= {PAID_MIN_DAYS}d paid); Apple's trial flag is not")
    print("stored by the backend. The `spans` column is the evidence behind each verdict.")
    print()


def write_csv(rows, path: Path):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["userId", "status", "renewal", "signupUtc",
                    "latestEndUtc", "daysRemaining", "product", "grantSpansDays"])
        for r in rows:
            w.writerow([
                r["userId"], r["status"], r["renewal"],
                r["created"].isoformat() if r["created"] else "",
                r["end"].isoformat() if r["end"] else "",
                "" if r["days_remaining"] is None else r["days_remaining"],
                r["product"],
                " ".join(f"{s:.2f}" for s in r["spans"]),
            ])
    print(f"Wrote {path}")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="production", choices=["production", "staging"])
    parser.add_argument("--since", default=None,
                        help="Only users whose createdDatetime >= this ISO-8601 instant, "
                             "e.g. 2026-09-06T02:33:00.000. Applies to the listing AND the "
                             "conversion percentages.")
    parser.add_argument("--exclude", nargs="*", default=TEST_ACCOUNT_SUBSTRINGS,
                        help=f"Email substrings to exclude (case-insensitive). "
                             f"Default: {TEST_ACCOUNT_SUBSTRINGS} plus the non-customer "
                             f"accounts {sorted(NON_CUSTOMER_EMAILS)}.")
    parser.add_argument("--exclude-name", nargs="*", default=[],
                        help="Full-name substrings to exclude (case-insensitive).")
    parser.add_argument("--include-sandbox", action="store_true",
                        help="Keep StoreKit sandbox grants (excluded by default).")
    parser.add_argument("--timezone", default=REPORT_TIMEZONE,
                        help=f"IANA zone for displayed times. Default: {REPORT_TIMEZONE}.")
    parser.add_argument("--csv", type=Path, default=None, help="Also write a CSV here.")
    args = parser.parse_args()

    since = None
    if args.since:
        since = parse_dt(args.since)
        if since is None:
            print(f"Could not parse --since {args.since!r}; expected ISO-8601 "
                  f"(e.g. 2026-09-06T02:33:00.000)", file=sys.stderr)
            return 2

    try:
        dynamodb = boto3.Session(region_name=REGION).resource("dynamodb")
        users = scan_users(dynamodb, args.env, args.exclude, args.exclude_name)
        # Exact-match drop, separate from the substring filter: these are real addresses that
        # must not be matched loosely.
        users = [u for u in users
                 if (u.get("email") or "").lower() not in NON_CUSTOMER_EMAILS]
        grants_by_user = scan_grants(dynamodb, args.env, args.include_sandbox)
        renewal = auto_renew_state(dynamodb, args.env)
        props_by_user = {p["userId"]: p
                         for p in scan_table(dynamodb, table_name(args.env, "user-properties"))}
    except NoCredentialsError:
        print("No AWS credentials found.", file=sys.stderr)
        return 1
    except ClientError as e:
        print(f"DynamoDB error: {e}", file=sys.stderr)
        return 1

    if since is not None:
        users = [u for u in users if u["created"] and u["created"] >= since]

    # Naive UTC, matching what parse_dt returns — every comparison in this script is
    # between two naive-UTC datetimes.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for u in users:
        grants = grants_by_user.get(u["userId"]) or []
        status, spans, end, days_remaining, product, txn = classify(grants, now)
        # Only meaningful while a subscription is live; a lapsed one renews nothing either way.
        if status in ("ON TRIAL", "PREMIUM"):
            label = {"off": "CANCELLED", "on": "on track"}.get(renewal.get(txn), "on track")
        else:
            label = "—"
        rows.append({**u, "status": status, "spans": spans, "end": end,
                     "days_remaining": days_remaining, "product": product,
                     "renewal": label})

    cohort_ids = {u["userId"] for u in users}
    stats = conversion_stats(cohort_ids, grants_by_user, now, renewal)
    oldest = min((u["created"] for u in users if u["created"]), default=None)
    act = activation_stats(cohort_ids, props_by_user, oldest)
    grant_count = sum(len(grants_by_user.get(u["userId"], [])) for u in users)
    print_report(rows, stats, args.env, since, args.include_sandbox, grant_count, act,
                 tz_name=args.timezone)

    if args.csv:
        write_csv(rows, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
