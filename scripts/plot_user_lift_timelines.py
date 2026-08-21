#!/usr/bin/env python3
"""Plot per-user lift-set activity timelines from DynamoDB.

READ-ONLY against DynamoDB: this script only performs `scan` (users) and `query`
(lift-sets) operations. It never writes, updates, or deletes any table item. The
only thing it writes to disk is the output PDF asset.

What it does
------------
1. Scans the users table and drops any user whose email contains an excluded
   substring, or whose full name matches an excluded name (both case-insensitive).
2. For each remaining user, queries their (non-deleted) lift-sets.
3. Keeps only users who have at least one lift-set.
4. Produces a single multi-page PDF:
     - Overview sheet 1: a swimlane -- one row per user with logged sets, sorted by
       volume, account-creation marker plus a dot per lift-set (+ sets/tier/push cols).
     - Overview sheet 2: users who signed up but logged no sets.
     - "Effort & e1RM by Exercise" pages, ONLY for users who unlocked their starting
       strength tier AND logged >5 sets: per exercise, each set's %1RM over time inside
       shaded effort bands (easy/moderate/hard/near_max/progress), plus per-lift + overall
       strength tier. Replicates the app's Epley/effort/tier math.
     - With --per-user: one weight-over-time scatter page per user.

Usage
-----
    # from WeightApp-backend/ (default env is production, per the ask)
    pip install matplotlib          # one-time, if not already present
    python scripts/plot_user_lift_timelines.py
    python scripts/plot_user_lift_timelines.py --env staging
    python scripts/plot_user_lift_timelines.py --exclude zach chloe internal
    python scripts/plot_user_lift_timelines.py --limit 25        # first N eligible users (debugging)
    python scripts/plot_user_lift_timelines.py --no-effort       # skip the Effort & e1RM pages
    python scripts/plot_user_lift_timelines.py --out /tmp/timelines.pdf

Output defaults to  WeightApp-backend/plots/user_lift_timelines_<env>.pdf
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, NoCredentialsError

REGION = "us-west-1"
PROJECT = "liftthebull"
DEFAULT_EXCLUSIONS = ["zmitc002", "zach", "chloe", "review", "marymitchell1212", "tjalves57"]
DEFAULT_NAME_EXCLUSIONS = ["john apple", "nicole pierce"]
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "plots"


# --------------------------------------------------------------------------- #
# DynamoDB (read-only)
# --------------------------------------------------------------------------- #
def table_name(env: str, suffix: str) -> str:
    return f"{PROJECT}-{env}-{suffix}"


def parse_dt(value):
    """Parse an ISO-8601 string into a naive UTC datetime (safe for plotting)."""
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def scan_users(dynamodb, env: str, exclusions, name_exclusions=None):
    """Scan the users table, returning [(userId, email, createdDatetime)] after exclusion.

    Drops a user if their email contains any `exclusions` substring OR their
    display name contains any `name_exclusions` substring (both case-insensitive).
    """
    table = dynamodb.Table(table_name(env, "users"))
    excl = [e.lower() for e in exclusions]
    name_excl = [n.lower() for n in (name_exclusions or [])]
    users = []
    # "name" is a DynamoDB reserved word, so alias it. "fullName" (Apple Sign-In)
    # and "name" (email/password register) are the two places a display name lives.
    scan_kwargs = {
        "ProjectionExpression": "userId, emailAddress, createdDatetime, fullName, #nm",
        "ExpressionAttributeNames": {"#nm": "name"},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            email = (item.get("emailAddress") or "").lower()
            if any(x in email for x in excl):
                continue
            name = (item.get("fullName") or item.get("name") or "").strip()
            if name and any(n in name.lower() for n in name_excl):
                continue
            users.append(
                {
                    "userId": item["userId"],
                    "email": item.get("emailAddress", ""),
                    "name": name,
                    "created": parse_dt(item.get("createdDatetime")),
                }
            )
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return users


def scan_user_properties(dynamodb, env: str):
    """Scan user-properties -> {userId: {tier, apns, bodyweight, sex}}. Read-only.

    tier       = hasMetStrengthTierConditions (starting strength tier unlocked).
    apns       = an apnsDeviceToken is present (push registered).
    bodyweight = float lbs, or None (needed for strength-tier math).
    sex        = "male"/"female" lowercased, or None.
    """
    table = dynamodb.Table(table_name(env, "user-properties"))
    props = {}
    scan_kwargs = {
        "ProjectionExpression": "userId, hasMetStrengthTierConditions, apnsDeviceToken, "
                                "bodyweight, biologicalSex"
    }
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            bw = item.get("bodyweight")
            sex = item.get("biologicalSex")
            props[item["userId"]] = {
                "tier": item.get("hasMetStrengthTierConditions") is True,
                "apns": bool(item.get("apnsDeviceToken")),
                "bodyweight": float(bw) if bw is not None else None,
                "sex": sex.lower() if isinstance(sex, str) else None,
            }
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return props


def query_lift_sets(dynamodb, env: str, user_id: str):
    """Query all non-deleted lift-sets for a user. Returns list of dicts sorted by date."""
    table = dynamodb.Table(table_name(env, "lift-sets"))
    sets = []
    query_kwargs = {"KeyConditionExpression": Key("userId").eq(user_id)}
    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get("Items", []):
            if item.get("deleted") is True:
                continue
            dt = parse_dt(item.get("createdDatetime"))
            if dt is None:
                continue
            weight = item.get("weight")
            sets.append(
                {
                    "date": dt,
                    "liftSetId": item.get("liftSetId", ""),
                    "weight": float(weight) if weight is not None else None,
                    "reps": int(item["reps"]) if item.get("reps") is not None else None,
                    "exerciseId": item.get("exerciseId", ""),
                    "baseline": item.get("isBaselineSet") is True,
                }
            )
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    sets.sort(key=lambda s: s["date"])
    return sets


def query_estimated_1rm(dynamodb, env: str, user_id: str):
    """Query non-deleted estimated-1rm records -> {liftSetId: {value, exerciseId, date}}.

    Read-only. `value` is the running-max e1RM snapshot recorded per logged set,
    joined back to its lift-set by liftSetId (the table's sort key).
    """
    table = dynamodb.Table(table_name(env, "estimated-1rm"))
    by_set = {}
    query_kwargs = {"KeyConditionExpression": Key("userId").eq(user_id)}
    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get("Items", []):
            if item.get("deleted") is True:
                continue
            val = item.get("value")
            by_set[item.get("liftSetId", "")] = {
                "value": float(val) if val is not None else None,
                "exerciseId": item.get("exerciseId", ""),
                "date": parse_dt(item.get("createdDatetime")),
            }
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return by_set


def query_exercises(dynamodb, env: str, user_id: str):
    """Query a user's exercises -> {exerciseItemId: {name, loadType}}. Read-only."""
    table = dynamodb.Table(table_name(env, "exercises"))
    by_id = {}
    query_kwargs = {"KeyConditionExpression": Key("userId").eq(user_id)}
    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get("Items", []):
            if item.get("deleted") is True:
                continue
            by_id[item.get("exerciseItemId", "")] = {
                "name": item.get("name", "Unknown"),
                "loadType": item.get("loadType", ""),
            }
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return by_id


# --------------------------------------------------------------------------- #
# App-logic ports: Epley 1RM, effort bands, strength tiers
# --------------------------------------------------------------------------- #
# Effort colors (WeightApp Color+Theme.swift).
EFFORT_COLORS = {
    "easy": "#22C55E",
    "moderate": "#21B7C9",
    "hard": "#5B3BE8",
    "near_max": "#FF6B35",
    "progress": "#FFB000",
}
EFFORT_ORDER = ["easy", "moderate", "hard", "near_max", "progress"]
EFFORT_BAND_LABEL = {
    "easy": "easy <70%",
    "moderate": "moderate 70–82%",
    "hard": "hard 82–92%",
    "near_max": "near max 92–100%",
    "progress": "progress (PR) >100%",
}

# Fundamental exercise IDs (deterministic) -> tier-table name keys.
FUNDAMENTAL_NAMES_BY_ID = {
    "00000000-0000-0000-0001-000000000001": "Deadlifts",
    "00000000-0000-0000-0001-000000000002": "Squats",
    "00000000-0000-0000-0001-000000000003": "Bench Press",
    "00000000-0000-0000-0001-000000000004": "Overhead Press",
    "00000000-0000-0000-0001-000000000005": "Barbell Rows",
}

# StrengthTier ordering / display (StrengthTierDefinitions.swift).
TIER_ORDER = ["none", "novice", "beginner", "intermediate", "advanced", "elite", "legend"]
TIER_TITLE = {t: t.capitalize() for t in TIER_ORDER}
TIER_COLOR = {
    "none": "#4d4d4d",
    "novice": "#ffffff",
    "beginner": "#4a8fd9",
    "intermediate": "#21b7c9",
    "advanced": "#21c55e",
    "elite": "#ffc850",
    "legend": "#cc85f5",
}

# Bodyweight-multiplier thresholds (min of each tier band), per lift x sex.
# Ported verbatim from StrengthTierData.thresholds (all isAbsolute:false).
TIER_THRESHOLDS = {
    "Squats": {
        "male": {"novice": 0, "beginner": 0.75, "intermediate": 1.25, "advanced": 1.75, "elite": 2.5, "legend": 3.0},
        "female": {"novice": 0, "beginner": 0.5, "intermediate": 1.0, "advanced": 1.5, "elite": 1.75, "legend": 2.25},
    },
    "Bench Press": {
        "male": {"novice": 0, "beginner": 0.5, "intermediate": 1.0, "advanced": 1.5, "elite": 2.0, "legend": 2.25},
        "female": {"novice": 0, "beginner": 0.25, "intermediate": 0.5, "advanced": 0.75, "elite": 1.0, "legend": 1.25},
    },
    "Deadlifts": {
        "male": {"novice": 0, "beginner": 1.0, "intermediate": 1.5, "advanced": 2.25, "elite": 3.0, "legend": 3.5},
        "female": {"novice": 0, "beginner": 0.5, "intermediate": 1.0, "advanced": 1.75, "elite": 2.25, "legend": 3.0},
    },
    "Barbell Rows": {
        "male": {"novice": 0, "beginner": 0.5, "intermediate": 0.75, "advanced": 1.0, "elite": 1.5, "legend": 1.75},
        "female": {"novice": 0, "beginner": 0.25, "intermediate": 0.40, "advanced": 0.65, "elite": 0.90, "legend": 1.20},
    },
    "Overhead Press": {
        "male": {"novice": 0, "beginner": 0.40, "intermediate": 0.55, "advanced": 0.80, "elite": 1.05, "legend": 1.35},
        "female": {"novice": 0, "beginner": 0.20, "intermediate": 0.35, "advanced": 0.55, "elite": 0.75, "legend": 1.00},
    },
}


def estimate_1rm(weight, reps):
    """Epley (WeightApp OneRMCalculator.estimate1RM)."""
    if weight is None or reps is None or reps <= 0:
        return 0.0
    if reps == 1:
        return weight
    return weight * (1.0 + reps / 30.0)


def bucket_from_percent(p):
    """Fraction of e1RM -> effort band (TrendsCalculator.IntensityBucket.from)."""
    if p >= 0.92:
        return "near_max"
    if p >= 0.82:
        return "hard"
    if p >= 0.70:
        return "moderate"
    return "easy"


def bucket_from_reps(reps):
    """Zero-weight sets are classified by reps in the app."""
    if reps is None:
        return "easy"
    if reps >= 12:
        return "near_max"
    if reps >= 9:
        return "hard"
    if reps >= 6:
        return "moderate"
    return "easy"


def classify_exercise_sets(ex_sets, e1rm_by_set):
    """Return the sets annotated with effort bucket + percent, matching the app.

    ex_sets: this exercise's sets, chronological. e1rm_by_set: liftSetId -> record
    (value = running-max e1RM snapshot for the linked set). Walks chronologically,
    tracking the established e1RM before each set.
    """
    established = 0.0
    out = []
    for s in ex_sets:
        w, r = s["weight"], s["reps"]
        rec = e1rm_by_set.get(s.get("liftSetId"))
        joined = rec["value"] if rec and rec["value"] is not None else None
        epley = estimate_1rm(w, r)

        if w is not None and w == 0:
            bucket, pct = bucket_from_reps(r), None
        elif s["baseline"]:
            denom = joined if joined else epley
            pct = (epley / denom) if denom else 0.0
            bucket = bucket_from_percent(pct)
        elif established > 0:
            if epley > established + 1e-4:
                bucket, pct = "progress", epley / established
            else:
                pct = epley / established
                bucket = bucket_from_percent(pct)
        else:
            # No established e1RM yet (first weighted set) — seed from its own value.
            denom = joined if joined else epley
            pct = (epley / denom) if denom else 1.0
            bucket = bucket_from_percent(pct)

        out.append({**s, "epley": epley, "bucket": bucket, "pct": pct})
        established = max(established, joined or 0.0, epley)
    return out


def tier_for_exercise(name, e1rm, bodyweight, sex):
    """Highest tier whose min*bodyweight <= e1rm (StrengthTierData.tierForExercise)."""
    table = TIER_THRESHOLDS.get(name, {}).get(sex)
    if not table or bodyweight <= 0:
        return "none"
    for tier in reversed(TIER_ORDER):
        if tier == "none":
            continue
        mult = table.get(tier)
        if mult is not None and e1rm >= mult * bodyweight:
            return tier
    return "novice"


def overall_tier(fundamental_e1rms, bodyweight, sex):
    """Min tier across the five fundamentals; missing lift -> none."""
    lowest_idx = TIER_ORDER.index("legend")
    for name in FUNDAMENTAL_NAMES_BY_ID.values():
        e1rm = fundamental_e1rms.get(name)
        tier = tier_for_exercise(name, e1rm, bodyweight, sex) if e1rm else "none"
        lowest_idx = min(lowest_idx, TIER_ORDER.index(tier))
    return TIER_ORDER[lowest_idx]


def _attach_effort_data(u, e1rm_by_set, exercises_meta, min_sets=2):
    """Compute per-exercise classified sets, current e1RM, and tiers; attach to user u.

    Groups the user's sets by exercise, classifies each set's effort against the
    established e1RM at that time, resolves per-fundamental + overall strength tier
    (with app-default bodyweight 200 / sex male when missing), and stores the result
    on u["effort_exercises"] / u["overall_tier"].
    """
    groups = {}
    for s in u["sets"]:
        groups.setdefault(s.get("exerciseId", ""), []).append(s)

    def _current_e1rm(ex_sets):
        cur = 0.0
        for s in ex_sets:
            rec = e1rm_by_set.get(s.get("liftSetId"))
            v = rec["value"] if rec and rec["value"] is not None else estimate_1rm(s["weight"], s["reps"])
            cur = max(cur, v or 0.0)
        return cur

    # Resolve bodyweight / sex with the app's dominant fallbacks.
    bw, sex = u.get("bodyweight"), u.get("sex")
    bw_used = bw if (bw and bw > 0) else 200.0
    sex_used = sex if sex in ("male", "female") else "male"
    u["bw_defaulted"] = not (bw and bw > 0)
    u["sex_defaulted"] = sex not in ("male", "female")
    u["bodyweight_used"], u["sex_used"] = bw_used, sex_used

    fundamental_e1rms = {}
    exercises = []
    for ex_id, ex_sets in groups.items():
        ex_sets.sort(key=lambda s: s["date"])
        fund_name = FUNDAMENTAL_NAMES_BY_ID.get((ex_id or "").lower())
        is_fund = fund_name is not None
        cur = _current_e1rm(ex_sets)
        if is_fund and cur > 0:
            fundamental_e1rms[fund_name] = cur
        if len(ex_sets) < min_sets:
            continue
        exercises.append({
            "name": fund_name or exercises_meta.get(ex_id, {}).get("name", "Unknown"),
            "is_fundamental": is_fund,
            "fund_name": fund_name,
            "current_e1rm": cur,
            "sets": classify_exercise_sets(ex_sets, e1rm_by_set),
            "count": len(ex_sets),
            "tier": (tier_for_exercise(fund_name, cur, bw_used, sex_used)
                     if is_fund and cur > 0 else None),
        })

    fund_order = list(FUNDAMENTAL_NAMES_BY_ID.values())
    exercises.sort(key=lambda ex: (0, fund_order.index(ex["fund_name"]))
                   if ex["is_fundamental"] else (1, -ex["count"]))

    u["effort_exercises"] = exercises
    u["overall_tier"] = overall_tier(fundamental_e1rms, bw_used, sex_used)


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #
def build_pdf(users_with_sets, users_without_sets, out_path: Path, env: str,
              per_user: bool = False, effort: bool = True):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Single "now" for the whole document so every page's right edge lines up.
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with PdfPages(out_path) as pdf:
        if users_with_sets:
            top = max(len(u["sets"]) for u in users_with_sets)
            _overview_page(
                pdf, users_with_sets, now, plt, mdates,
                title=f"Lift-set Activity  ·  {env}",
                subtitle=f"{len(users_with_sets)} users with logged sets, sorted by volume "
                         f"(top user: {top} sets)",
            )
        if users_without_sets:
            _overview_page(
                pdf, users_without_sets, now, plt, mdates,
                title=f"Signed Up — No Sets Logged  ·  {env}",
                subtitle=f"{len(users_without_sets)} users created an account but have logged "
                         f"no sets (newest first)",
            )
        if effort:
            for u in users_with_sets:
                if u.get("effort_exercises"):
                    _effort_page(pdf, u, now, plt, mdates)
        if per_user:
            for u in users_with_sets:
                _user_page(pdf, u, now, plt, mdates)

    return out_path


def _draw_now(ax, now, mdates):
    """Vertical 'Today' reference line + label, and pin the right edge to now."""
    ax.axvline(now, color="#486581", linestyle=(0, (2, 2)), linewidth=1.1, zorder=2)
    ax.annotate(
        f"Today · {now:%Y-%m-%d}",
        xy=(now, 1), xycoords=("data", "axes fraction"),
        xytext=(-4, -4), textcoords="offset points",
        rotation=90, ha="right", va="top", fontsize=7.5,
        # "bold" (700), not "600": DejaVu Sans ships only 400 and 700, so matplotlib was
        # silently rounding 600 up to 700 and logging a warning for every call. This
        # asks for what it was always going to render.
        color="#486581", fontweight="bold",
    )
    left_num, right_num = ax.get_xlim()
    now_num = mdates.date2num(now)
    pad = max((right_num - left_num) * 0.03, 1.0)
    ax.set_xlim(right=max(right_num, now_num) + pad)


def _label_for(u):
    """Display name: fullName/name if present, else email."""
    name = (u.get("name") or "").strip()
    if name:
        return name
    return u.get("email") or "(no email)"


def _truncate(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _overview_page(pdf, users, now, plt, mdates, title, subtitle):
    """Swimlane: one row per user (name + userId), creation marker + a dot per set."""
    # Palette
    ink = "#102a43"
    sub = "#627d98"
    dim = "#9fb3c8"
    band = "#f5f7fa"
    line = "#d9e2ec"
    dot = "#2f6fed"
    baseline_c = "#e8a33d"   # baseline (strength-tier) sets — amber
    created_c = "#e8543f"
    accent = "#334e68"
    star_c = "#f5a623"
    apns_c = "#2f9e44"   # push token present — green check

    n = len(users)
    row_h = 0.46
    height = max(3.6, min(row_h * n + 1.9, 60.0))
    fig, ax = plt.subplots(figsize=(13, height))

    # Roomy top margin so title / subtitle / legend stack without colliding.
    title_in, bottom_in = 2.2, 0.65
    fig.subplots_adjust(left=0.29, right=0.90,
                        top=1 - title_in / height, bottom=bottom_in / height)

    # Font sizes scale down as the roster grows.
    name_fs = 8.5 if n <= 30 else (7.0 if n <= 60 else 6.0)
    id_fs = max(4.5, name_fs - 2.5)

    # Stable sort: with-sets pages order by volume; no-set pages keep the
    # caller's ordering (all counts equal), e.g. newest signups first.
    ordered = sorted(users, key=lambda u: len(u["sets"]), reverse=True)

    for i, u in enumerate(ordered):
        y = n - 1 - i  # highest-count row at the top
        if i % 2 == 0:
            ax.axhspan(y - 0.5, y + 0.5, color=band, zorder=0, linewidth=0)

        set_dates = [s["date"] for s in u["sets"]]
        # Draw the creation marker as a short vertical tick spanning the row, then
        # the set dots on top, so a user who signed up and logged sets on the same
        # day still shows their sets rather than hiding them under the marker.
        if u["created"]:
            ax.vlines(u["created"], y - 0.42, y + 0.42,
                      color=created_c, linewidth=1.4, zorder=3)
        if set_dates:
            ax.plot([min(set_dates), max(set_dates)], [y, y],
                    color=line, linewidth=1.3, solid_capstyle="round", zorder=1)
            normal = [s["date"] for s in u["sets"] if not s["baseline"]]
            base = [s["date"] for s in u["sets"] if s["baseline"]]
            if normal:
                ax.scatter(normal, [y] * len(normal),
                           s=14, color=dot, alpha=0.9, edgecolor="white", linewidth=0.3, zorder=5)
            if base:
                # Baseline (strength-tier) sets in amber, drawn on top.
                ax.scatter(base, [y] * len(base),
                           s=22, color=baseline_c, alpha=0.95, edgecolor="white", linewidth=0.4, zorder=6)

        # Left gutter: name (bold) over userId (dim monospace), right-aligned.
        ax.text(-0.014, y + 0.16, _truncate(_label_for(u), 30),
                transform=ax.get_yaxis_transform(), ha="right", va="center",
                fontsize=name_fs, fontweight="bold", color=ink)
        ax.text(-0.014, y - 0.19, u["userId"],
                transform=ax.get_yaxis_transform(), ha="right", va="center",
                fontsize=id_fs, color=dim, family="monospace")

        # Right gutter columns: set count, tier star, push check.
        ax.text(1.045, y, str(len(u["sets"])),
                transform=ax.get_yaxis_transform(), ha="right", va="center",
                fontsize=name_fs, fontweight="700", color=accent)
        if u.get("tier_unlocked"):
            ax.text(1.085, y, "★",
                    transform=ax.get_yaxis_transform(), ha="center", va="center",
                    fontsize=name_fs + 1.5, color=star_c)
        ax.text(1.125, y, "✓" if u.get("has_apns") else "·",
                transform=ax.get_yaxis_transform(), ha="center", va="center",
                fontsize=name_fs + (0 if u.get("has_apns") else 2),
                fontweight="700" if u.get("has_apns") else "normal",
                color=apns_c if u.get("has_apns") else dim)

    ax.set_ylim(-0.6, n - 0.4)
    ax.set_yticks([])
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#eef2f6", linewidth=0.8)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(line)
    ax.tick_params(axis="x", colors=sub, labelsize=8, length=0)

    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

    _draw_now(ax, now, mdates)

    # Gutter column headers, just above the top row.
    ax.text(1.045, n - 0.35, "sets", transform=ax.get_yaxis_transform(),
            ha="right", va="bottom", fontsize=id_fs, fontweight="700", color=dim)
    ax.text(1.085, n - 0.35, "tier", transform=ax.get_yaxis_transform(),
            ha="center", va="bottom", fontsize=id_fs, fontweight="700", color=dim)
    ax.text(1.125, n - 0.35, "push", transform=ax.get_yaxis_transform(),
            ha="center", va="bottom", fontsize=id_fs, fontweight="700", color=dim)

    # Header block (figure coords so it sits above the plot cleanly).
    from matplotlib.lines import Line2D

    left_x = 0.29
    # Stacked header (inch offsets from the top so spacing is height-independent):
    #   line 1 title, line 2 subtitle, line 3 legend row.
    fig.text(left_x, 1 - 0.45 / height, title,
             ha="left", va="top", fontsize=15, fontweight="700", color=ink)
    fig.text(left_x, 1 - 0.86 / height, subtitle,
             ha="left", va="top", fontsize=9.5, color=sub)

    handles = [
        Line2D([0], [0], color=created_c, lw=1.6, label="account created"),
        Line2D([0], [0], marker="o", linestyle="none", markerfacecolor=dot,
               markeredgecolor="white", markeredgewidth=0.3, markersize=6, label="set logged"),
        Line2D([0], [0], marker="o", linestyle="none", markerfacecolor=baseline_c,
               markeredgecolor="white", markeredgewidth=0.4, markersize=7, label="baseline set"),
        Line2D([0], [0], marker="*", linestyle="none", markerfacecolor=star_c,
               markeredgecolor=star_c, markersize=11, label="★ tier: starting strength tier unlocked"),
        Line2D([0], [0], marker=r"$\checkmark$", linestyle="none", markerfacecolor=apns_c,
               markeredgecolor=apns_c, markersize=9, label="✓ push: apns token present"),
        Line2D([0], [0], color="#486581", linestyle=(0, (2, 2)), lw=1.1, label="today"),
    ]
    fig.legend(handles=handles, loc="upper left",
               bbox_to_anchor=(left_x, 1 - 1.22 / height), ncol=3,
               frameon=False, fontsize=8, handletextpad=0.4, columnspacing=1.6)

    pdf.savefig(fig)
    plt.close(fig)


def _user_page(pdf, u, now, plt, mdates):
    """One page: weight-over-time scatter + account-creation line."""
    sets = u["sets"]
    fig, ax = plt.subplots(figsize=(11, 6))

    dates = [s["date"] for s in sets]
    dot_c, baseline_c = "#1f77b4", "#e8a33d"
    with_w = [s for s in sets if s["weight"] is not None]

    if with_w:
        norm = [s for s in with_w if not s["baseline"]]
        base = [s for s in with_w if s["baseline"]]
        if norm:
            ax.scatter([s["date"] for s in norm], [s["weight"] for s in norm],
                       s=22, color=dot_c, alpha=0.75, edgecolor="none", zorder=3, label="Lift-set")
        if base:
            ax.scatter([s["date"] for s in base], [s["weight"] for s in base],
                       s=36, color=baseline_c, alpha=0.9, edgecolor="white", linewidth=0.4,
                       zorder=4, label="Baseline set")
        ax.set_ylabel("Weight")
    else:
        # No weights recorded -- fall back to a pure activity rug at a fixed level.
        norm = [s["date"] for s in sets if not s["baseline"]]
        base = [s["date"] for s in sets if s["baseline"]]
        if norm:
            ax.scatter(norm, [1] * len(norm), s=22, color=dot_c, alpha=0.75, zorder=3, label="Lift-set")
        if base:
            ax.scatter(base, [1] * len(base), s=36, color=baseline_c, alpha=0.9,
                       edgecolor="white", linewidth=0.4, zorder=4, label="Baseline set")
        ax.set_yticks([])
        ax.set_ylabel("Lift-sets (no weight recorded)")

    if u["created"]:
        ax.axvline(
            u["created"], color="#d62728", linestyle="--", linewidth=1.4, zorder=2,
            label="Account created",
        )

    span = ""
    if dates:
        span = f"{min(dates):%Y-%m-%d} → {max(dates):%Y-%m-%d}"
    created_str = f"{u['created']:%Y-%m-%d}" if u["created"] else "unknown"
    tier = "  |  ★ starting strength tier unlocked" if u.get("tier_unlocked") else ""
    ax.set_title(
        f"{_label_for(u)}\n"
        f"userId {u['userId'][:8]}...  |  {len(sets)} lift-sets  |  created {created_str}  |  {span}{tier}",
        fontsize=10,
    )
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.set_xlabel("Date")
    _draw_now(ax, now, mdates)
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Effort & e1RM pages (tier-unlocked power users)
# --------------------------------------------------------------------------- #
# Horizontal effort-band zones (%1RM) and midpoints for zero-weight sets.
_BAND_SPANS = [("easy", 0, 70), ("moderate", 70, 82), ("hard", 82, 92),
               ("near_max", 92, 100), ("progress", 100, 118)]
_BAND_MID = {"easy": 35, "moderate": 76, "hard": 87, "near_max": 96, "progress": 109}


def _effort_page(pdf, user, now, plt, mdates):
    exercises = user.get("effort_exercises", [])
    if not exercises:
        return
    from matplotlib.lines import Line2D

    per_page = 4  # 2 x 2
    chunks = [exercises[i:i + per_page] for i in range(0, len(exercises), per_page)]
    n_pages = len(chunks)
    for pidx, chunk in enumerate(chunks):
        fig, axes = plt.subplots(2, 2, figsize=(13, 9.5))
        axes = axes.flatten()
        fig.subplots_adjust(left=0.07, right=0.965, top=0.80, bottom=0.07,
                            hspace=0.46, wspace=0.18)
        _effort_header(fig, user, pidx, n_pages)
        _effort_legend(fig, Line2D)
        for ax, ex in zip(axes, chunk):
            _effort_subplot(ax, ex, now, mdates)
        for ax in axes[len(chunk):]:
            ax.axis("off")
        pdf.savefig(fig)
        plt.close(fig)


def _effort_header(fig, user, pidx, n_pages):
    ink, sub = "#102a43", "#627d98"
    tier = user.get("overall_tier", "none")
    tcolor = TIER_COLOR.get(tier, "#888888")
    bw, sex = int(user.get("bodyweight_used", 200)), user.get("sex_used", "male")
    bwnote = " (default)" if user.get("bw_defaulted") else ""
    sexnote = " (default)" if user.get("sex_defaulted") else ""

    fig.text(0.07, 0.965, "Effort & e1RM by Exercise", fontsize=16,
             fontweight="700", color=ink, ha="left", va="top")
    fig.text(0.07, 0.935, f"{_label_for(user)}   ·   {user['userId'][:8]}…   ·   "
             f"{len(user['sets'])} sets", fontsize=10, color=sub, ha="left", va="top")

    fig.text(0.965, 0.965, f"★ {TIER_TITLE.get(tier, 'None')}", fontsize=14,
             fontweight="700", color=tcolor, ha="right", va="top")
    fig.text(0.965, 0.936, f"BW {bw} lb{bwnote}  ·  {sex}{sexnote}",
             fontsize=9, color=sub, ha="right", va="top")
    if n_pages > 1:
        fig.text(0.965, 0.914, f"page {pidx + 1}/{n_pages}",
                 fontsize=8, color=sub, ha="right", va="top")


def _effort_legend(fig, Line2D):
    handles = [
        Line2D([0], [0], marker="o", linestyle="none", markerfacecolor=EFFORT_COLORS[b],
               markeredgecolor="none", markersize=8, label=EFFORT_BAND_LABEL[b])
        for b in EFFORT_ORDER
    ]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.895),
               ncol=5, frameon=False, fontsize=9, handletextpad=0.4, columnspacing=1.6)


def _effort_subplot(ax, ex, now, mdates):
    # Shaded effort-band zones.
    for band, lo, hi in _BAND_SPANS:
        ax.axhspan(lo, hi, color=EFFORT_COLORS[band], alpha=0.10, zorder=0, linewidth=0)

    for s in ex["sets"]:
        band = s["bucket"]
        reps = s["reps"] or 0
        size = 24 + min(reps, 20) * 7
        zero_w = s["weight"] is not None and s["weight"] == 0
        y = min(s["pct"] * 100, 116) if s["pct"] is not None else _BAND_MID.get(band, 50)
        ax.scatter(
            s["date"], y, s=size, marker=("D" if s["baseline"] else "o"),
            facecolor=("none" if zero_w else EFFORT_COLORS[band]),
            edgecolor=EFFORT_COLORS[band],
            linewidths=(1.3 if zero_w else 0.5), alpha=0.9, zorder=3,
        )

    # Label progress (PR) sets with weight×reps for absolute context.
    for s in ex["sets"]:
        if s["bucket"] == "progress" and s["weight"]:
            y = min((s["pct"] or 1.0) * 100, 116)
            ax.annotate(f"{int(s['weight'])}×{s['reps']}", (s["date"], y),
                        textcoords="offset points", xytext=(0, 6),
                        fontsize=6.5, color="#334e68", ha="center")

    ax.set_ylim(0, 118)
    ax.set_yticks([0, 70, 82, 92, 100])
    ax.set_yticklabels(["0", "70", "82", "92", "100"], fontsize=8)
    ax.set_ylabel("% of e1RM", fontsize=9)
    ax.tick_params(axis="x", labelsize=8, colors="#627d98")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    _draw_now(ax, now, mdates)

    # Title above the axes (name on top, e1RM + tier below) so nothing overlaps the data.
    tier_str = f"   ·   {TIER_TITLE.get(ex['tier'], ex['tier'])}" if ex.get("tier") else ""
    ax.set_title(f"{ex['name']}\ne1RM {int(ex['current_e1rm'])} lb{tier_str}",
                 fontsize=10.5, fontweight="bold", color="#102a43", loc="left")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="production", choices=["production", "staging"])
    parser.add_argument("--exclude", nargs="*", default=DEFAULT_EXCLUSIONS,
                        help="Email substrings to exclude (case-insensitive).")
    parser.add_argument("--exclude-name", nargs="*", default=DEFAULT_NAME_EXCLUSIONS,
                        help="Full-name substrings to exclude (case-insensitive).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only plot the first N eligible users (with lift-sets).")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output PDF path (default: plots/user_lift_timelines_<env>.pdf).")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not open the PDF automatically when done.")
    parser.add_argument("--per-user", action="store_true",
                        help="Also append one detail page per user (default: overview page only).")
    parser.add_argument("--no-effort", action="store_true",
                        help="Skip the per-exercise Effort & e1RM pages for tier-unlocked power users.")
    args = parser.parse_args()

    out_path = args.out or (OUTPUT_DIR / f"user_lift_timelines_{args.env}.pdf")

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("ERROR: matplotlib is required. Install it with:\n  pip install matplotlib", file=sys.stderr)
        return 1

    dynamodb = boto3.resource("dynamodb", region_name=REGION)

    print(f"[{args.env}] Scanning users (excluding emails containing: {', '.join(args.exclude)}"
          f"; names: {', '.join(args.exclude_name) or 'none'}) ...")
    try:
        users = scan_users(dynamodb, args.env, args.exclude, args.exclude_name)
    except NoCredentialsError:
        print("ERROR: No AWS credentials found. Configure your profile as you do for `make save-user-production`.",
              file=sys.stderr)
        return 1
    except ClientError as e:
        print(f"ERROR scanning users: {e}", file=sys.stderr)
        return 1
    print(f"  {len(users)} users after exclusions.")

    print("Loading tier + push flags from user-properties ...")
    try:
        user_props = scan_user_properties(dynamodb, args.env)
    except ClientError as e:
        print(f"  WARN: could not read user-properties ({e}); tier/push columns disabled.", file=sys.stderr)
        user_props = {}

    users_with_sets = []
    users_without_sets = []
    for i, u in enumerate(users, 1):
        try:
            sets = query_lift_sets(dynamodb, args.env, u["userId"])
        except ClientError as e:
            print(f"  WARN: query failed for {u['userId']}: {e}", file=sys.stderr)
            continue
        info = user_props.get(u["userId"], {})
        u["tier_unlocked"] = info.get("tier", False)
        u["has_apns"] = info.get("apns", False)
        u["bodyweight"] = info.get("bodyweight")
        u["sex"] = info.get("sex")
        u["sets"] = sets
        (users_with_sets if sets else users_without_sets).append(u)
        if i % 25 == 0:
            print(f"  ...checked {i}/{len(users)} users "
                  f"({len(users_with_sets)} with sets, {len(users_without_sets)} without)")

    print(f"  {len(users_with_sets)} users have lift-sets; "
          f"{len(users_without_sets)} signed up with none.")

    if not users_with_sets and not users_without_sets:
        print("Nothing to plot.")
        return 0

    # Sheet 1: with-sets, highest volume first.
    users_with_sets.sort(key=lambda u: len(u["sets"]), reverse=True)
    # Sheet 2: no-sets, newest signups first (stable through the in-page sort).
    users_without_sets.sort(key=lambda u: u["created"] or datetime.min, reverse=True)
    if args.limit is not None:
        users_with_sets = users_with_sets[: args.limit]
        users_without_sets = users_without_sets[: args.limit]
        print(f"  Limiting each sheet to first {args.limit} users (--limit).")

    # Effort & e1RM pages: only for tier-unlocked users with more than 5 sets.
    n_effort = 0
    if not args.no_effort:
        qualifying = [u for u in users_with_sets if u.get("tier_unlocked") and len(u["sets"]) > 5]
        print(f"Building Effort & e1RM data for {len(qualifying)} qualifying users "
              f"(tier unlocked + >5 sets) ...")
        for u in qualifying:
            try:
                e1rm_by_set = query_estimated_1rm(dynamodb, args.env, u["userId"])
                exercises_meta = query_exercises(dynamodb, args.env, u["userId"])
            except ClientError as e:
                print(f"  WARN: effort queries failed for {u['userId']}: {e}", file=sys.stderr)
                continue
            _attach_effort_data(u, e1rm_by_set, exercises_meta)
            if u.get("effort_exercises"):
                n_effort += 1

    total_sets = sum(len(u["sets"]) for u in users_with_sets)
    extra = "overview + per-user pages" if args.per_user else "overview pages only"
    print(f"Rendering {extra}: sheet 1 = {len(users_with_sets)} users ({total_sets} sets), "
          f"sheet 2 = {len(users_without_sets)} users with no sets, "
          f"effort pages = {n_effort} users ...")
    out = build_pdf(users_with_sets, users_without_sets, out_path, args.env,
                    per_user=args.per_user, effort=not args.no_effort)
    print(f"Done -> {out}")

    if not args.no_open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, str(out)], check=False)
        except FileNotFoundError:
            print(f"(Could not auto-open; open it manually: {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
