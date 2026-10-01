#!/usr/bin/env python3
"""Plot rolling subscription-funnel conversion rates over time.

READ-ONLY against DynamoDB: this script only performs `scan` operations on the users and
entitlement-grants tables. It never writes, updates, or deletes any table item. The only
thing it writes to disk is the output PDF.

The three pages
---------------
All three share one cohort, evaluated as-of an instant `t`:

    cohort       users who signed up in [t-14d, t-7d)

    page 1   free -> paid     of the cohort, holding a YEARLY paid grant begun in [t-7d, t]
    page 2   free -> trial    of the cohort, having started a free trial at any point up to t
    page 3   trial -> paid    of the cohort who started a trial, how many reached yearly paid

Seven-day-wide cohort, given a week to convert. Page 3 is page 1 split by its own funnel: a
falling page-1 line means either fewer people are starting trials or fewer trials are
converting, and only pages 2 and 3 can say which.

YEARLY ONLY on the paid side -- a monthly subscription counts as "still free", deliberately,
because the yearly plan is the one with a trial in front of it and so the one this funnel is
about. Trials need no product filter: every trial grant in the table is yearly, because yearly
is the only trial-eligible product.

The point of sampling hourly rather than quoting today's number is that a single value cannot
answer "is this getting better?". The shape can.

READ THE DENOMINATOR LINE BEFORE READING ANY RATE
-------------------------------------------------
The denominators are NOT stable across the series. The signup cohort bottoms out at 4 users
early on, so one conversion swings the rate by 25 points. Page 3 is thinner still -- its
denominator is only the trial-starters inside that cohort, often low single digits. A bare
percentage line would make that read as real volatility that later "settled down", when all
that happened is the denominator grew.

So every point carries a 95% Wilson confidence band, and the denominator is drawn on its own
axis. Where the band is wide, the number underneath it means nothing. Wilson rather than the
normal approximation specifically because these series spend their early life at n < 10 and at
zero counts, where the normal interval collapses to zero width and implies certainty that is
not there.

Two caveats on historical reconstruction
----------------------------------------
Grants rebuild exactly: creation is a conditional put and nothing in the system ever deletes
or reconciles a grant row, so past numerators are recoverable with full fidelity.

The users table, however, HARD-DELETES. Anyone who has since deleted their account is missing
from every historical cohort they belonged to, so denominators skew slightly small the further
back you look, which biases the plotted rates marginally upward in the past.

Usage
-----
    # from WeightApp-backend/
    pip install matplotlib          # one-time, if not already present
    make plot-conversion                        # production, hourly
    make plot-conversion ENV=staging
    make plot-conversion INTERVAL=6             # coarser sampling

    python scripts/plot_conversion_trend.py --interval-hours 24
    python scripts/plot_conversion_trend.py --inches-per-day 0.4   # narrower page
    python scripts/plot_conversion_trend.py --out /tmp/conv.pdf --no-open

Output defaults to  WeightApp-backend/plots/conversion_trend_<env>.pdf
"""

import argparse
import subprocess
import sys
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import boto3
from botocore.exceptions import NoCredentialsError

# Run as `python scripts/plot_conversion_trend.py` from the repo root, which puts `scripts/`
# on sys.path but not its parent -- so the package import below needs the parent added.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.plot_user_lift_timelines import (  # noqa: E402
    OUTPUT_DIR,
    REGION,
    parse_dt,
    scan_users,
    table_name,
)

# Narrow on purpose, matching report_subscription_status.py rather than the bare-"zach" list in
# plot_user_lift_timelines.py. That broader list once dropped `zacharydwight.mayfield@gmail.com`
# -- a real paying customer -- out of a revenue figure. A conversion chart has the same
# exposure: silently omitting subscribers understates the numerator.
TEST_ACCOUNT_SUBSTRINGS = ["zach+", "zachmitchell002"]
NON_CUSTOMER_EMAILS = {"review@liftthebull.io"}

# Reading a grant's purpose from its duration. This is the ONLY available discriminator: Apple
# reports a free trial via `offerType` on the notification and the backend does not persist
# that field, so there is no stored flag to read instead. Bounds are deliberately loose rather
# than exact matches on 7 and 365, so a retuned trial length or a proration still classifies.
TRIAL_MAX_DAYS = 14
PAID_MIN_DAYS = 28

# Cohort window: signed up between 14 and 7 days before the evaluation instant, then given the
# following 7 days to convert.
COHORT_START_DAYS = 14
COHORT_END_DAYS = 7

# Earliest signup data worth including. Everything before this predates the funnel being
# measured at all. The first evaluable instant is therefore COHORT_START_DAYS after it.
DEFAULT_EARLIEST = "2026-09-06T02:33:00"

Z_95 = 1.96

# Eras are defined on SIGNUP time: a user belongs to whichever era was running when their
# account was created. Add a row whenever something changes the KIND of user coming through
# the door -- ad spend, a release, a new channel. `start` is the first instant of that era;
# the first row's start is ignored (it runs from the beginning of time).
#
# THE CHART CANNOT SHADE THESE DIRECTLY. A point at instant `t` summarises a cohort that
# signed up in [t-14d, t-7d), so an era boundary only becomes visible 7 days later and is not
# fully in effect until 14. Shading on the raw boundary would credit the new era with a week of
# the old one's users.
#
# So the ribbon draws COMPOSITION rather than a single label per stretch: at each instant it
# stacks the share of that cohort belonging to each era. During the changeover the cohort
# genuinely contains both populations, and the proportion is the informative part. It also
# avoids a wrong answer the label approach gave — whether a cohort is "pure" depends on
# whether anyone actually signed up either side of the boundary, not on whether the window
# arithmetic straddles it.
ERAS = [
    ("Ads $30/day", None),
    ("Ads $60/day", "2026-09-23T21:31:51.108"),
    # Reverted after the $60 window produced 87 signups, 6 trials and 0 paying customers.
    # Labelled "restored" rather than reusing the first label on purpose: it is the same spend
    # but not the same conditions, because every budget change restarts Google's learning
    # phase. Collapsing them would assert an equivalence the data cannot support.
    ("Ads $30/day (restored)", "2026-09-30T17:54:00"),
]

RATE_COLOUR = "#4C78A8"
DENOM_COLOUR = "#8C8C8C"
THIN_COLOUR = "#C0392B"
# Ribbon colours, one per era in order. Muted on purpose: this is context behind the data,
# not a second data series competing with the line.
ERA_COLOURS = ["#5B8C5A", "#8E6C9B", "#C08B4A", "#4A7C94"]
MIXED_COLOUR = "#9E9E9E"
# Top ribbon geometry, in axes fraction.
RIBBON_BASE = 0.955
RIBBON_HEIGHT = 0.045


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
# One page each. `numer`/`denom` name keys produced by build_series, so a fourth page is a new
# entry here plus the count it needs -- not a new render function.
Metric = namedtuple("Metric", "title numer denom denom_label denom_short note")

METRICS = [
    Metric(
        title="Free → paid (yearly) conversion",
        numer="paid", denom="cohort",
        denom_label="Cohort size (users)", denom_short="cohort size",
        note=f"signed up {COHORT_START_DAYS}–{COHORT_END_DAYS}d before each point, "
             f"reached yearly paid within the following {COHORT_END_DAYS}d",
    ),
    Metric(
        title="Free → free trial conversion",
        numer="trial", denom="cohort",
        denom_label="Cohort size (users)", denom_short="cohort size",
        note=f"signed up {COHORT_START_DAYS}–{COHORT_END_DAYS}d before each point, "
             f"had started a free trial as of each point",
    ),
    Metric(
        title="Free trial → paid (yearly) conversion",
        numer="trial_paid", denom="trial",
        denom_label="Trial starters (users)", denom_short="trial starters",
        note=f"of the cohort who started a trial, reached yearly paid in the "
             f"{COHORT_END_DAYS}d before each point  ·  denominator is trial starters, "
             f"not signups — expect a wide band",
    ),
]


# --------------------------------------------------------------------------- #
# Data (read-only)
# --------------------------------------------------------------------------- #
def scan_grants(dynamodb, env: str):
    """-> (trial_starts, paid_starts), each {userId: [naive-UTC datetime, ...]}.

    `trial_starts` is any grant short enough to be a trial; `paid_starts` is any YEARLY grant
    long enough to be a real purchase. Sandbox grants are dropped -- they are test purchases
    and would inflate numerators against a denominator of real accounts.
    """
    table = dynamodb.Table(table_name(env, "entitlement-grants"))
    trials, paid = defaultdict(list), defaultdict(list)
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        for g in resp.get("Items", []):
            if g.get("transactionEnvironment") == "Sandbox":
                continue
            start, end = parse_dt(g.get("startUtc")), parse_dt(g.get("endUtc"))
            if start is None or end is None:
                continue
            span = (end - start).total_seconds() / 86400.0
            if span <= TRIAL_MAX_DAYS:
                trials[g["userId"]].append(start)
            elif span >= PAID_MIN_DAYS and "yearly" in str(g.get("productId", "")).lower():
                paid[g["userId"]].append(start)
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return dict(trials), dict(paid)


def era_bounds(eras):
    """Boundary instants, oldest first. The first era has no start (it runs from the beginning)."""
    return [b for b in (parse_dt(s) for _, s in eras[1:]) if b is not None]


def build_series(signups, trial_starts, paid_starts, earliest, now,
                 interval_hours, max_lookback_days, eras=None):
    """-> [{t, cohort, trial, paid, trial_paid}] sampled every `interval_hours`.

    Everything is evaluated against in-memory lists. The tables are scanned ONCE by the
    caller -- re-scanning per sample point would be thousands of identical scans of data
    that cannot change mid-run.

    `trial` counts trials begun at ANY time up to t, not inside a window. A trial starts within
    minutes of signing up, so windowing it the way the paid event is windowed would exclude
    every one of them. The paid event genuinely needs its window: it lands ~7 days after the
    trial begins, which is exactly what [t-7d, t] is positioned to catch.
    """
    first = earliest + timedelta(days=COHORT_START_DAYS)
    start = max(first, now - timedelta(days=max_lookback_days))

    # Which era each user signed up under. Computed once per user rather than per sample point.
    eras = eras or ERAS
    bounds = era_bounds(eras)
    era_of = {u: sum(1 for b in bounds if b <= c) for u, c in signups}

    points = []
    t = start
    step = timedelta(hours=interval_hours)
    while t <= now:
        lo = t - timedelta(days=COHORT_START_DAYS)
        hi = t - timedelta(days=COHORT_END_DAYS)
        cohort = [u for u, created in signups if lo <= created < hi]
        trial = paid = trial_paid = 0
        for u in cohort:
            started = any(s <= t for s in trial_starts.get(u, ()))
            bought = any(hi <= s <= t for s in paid_starts.get(u, ()))
            trial += started
            paid += bought
            trial_paid += started and bought
        # Era COMPOSITION of this cohort, not a single label. During the window after a
        # change the cohort really does contain both populations, and the proportion is the
        # interesting part — collapsing it to "mixed" throws away exactly what you want to see.
        counts = [0] * len(eras)
        for u in cohort:
            counts[era_of.get(u, 0)] += 1
        points.append({"t": t, "cohort": len(cohort), "trial": trial,
                       "paid": paid, "trial_paid": trial_paid, "era_counts": counts})
        t += step
    return points


def era_summary(points, eras, now):
    """-> [(label, first_seen, peak_share, peak_at, enters_at)] for the console.

    Reports COMPOSITION, matching what the ribbon draws. The older version of this classified
    each instant as belonging to one era or to a "mixed" bucket, which was both less
    informative and occasionally wrong: whether a cohort is pure depends on whether anyone
    actually signed up either side of the boundary, not on whether the window arithmetic
    straddles it. A 6.8-day era can still reach 100% of a 7-day window if the hour on the far
    side of its boundary happens to be empty.
    """
    bounds = era_bounds(eras)
    out = []
    for idx, (label, _) in enumerate(eras):
        seen = [(p["t"], p["era_counts"][idx] / (sum(p["era_counts"]) or 1))
                for p in points
                if idx < len(p.get("era_counts", [])) and p["era_counts"][idx] > 0]
        # An era's users first become measurable COHORT_END_DAYS after the era begins — that
        # is when the freshest of them age into the trailing edge of the window.
        enters = (bounds[idx - 1] + timedelta(days=COHORT_END_DAYS)) if idx else None
        if seen:
            peak_at, peak = max(seen, key=lambda x: x[1])
            out.append((label, seen[0][0], peak, peak_at, enters))
        else:
            out.append((label, None, 0.0, None, enters))
    return out


def scan_cancellations(dynamodb, env: str) -> dict:
    """-> {userId: earliest AUTO_RENEW_DISABLED instant}.

    EARLIEST, not latest: the question is whether they bailed out immediately, and a later
    re-enable followed by another cancel says nothing about that first hour. Tolerates the
    table being absent, which it is on a fresh environment.
    """
    from botocore.exceptions import ClientError
    out = {}
    try:
        table = dynamodb.Table(table_name(env, "subscription-events"))
        kwargs = {}
        while True:
            resp = table.scan(**kwargs)
            for e in resp.get("Items", []):
                if e.get("transactionEnvironment") == "Sandbox":
                    continue
                if e.get("notificationType") != "DID_CHANGE_RENEWAL_STATUS":
                    continue
                if e.get("subtype") != "AUTO_RENEW_DISABLED":
                    continue
                t = parse_dt(e.get("eventTimestamp"))
                u = e.get("userId")
                if u and t and (u not in out or t < out[u]):
                    out[u] = t
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    return out


def build_bucket_series(signups, trial_starts, cancels, earliest, now, bucket_hours,
                        attach_minutes, hold_minutes, trailing_days, eras):
    """-> [{t, n, k, trailing_n, trailing_k, era_counts}] in fixed buckets of signup time.

    A different shape from the rolling-cohort pages and deliberately so. Those answer "of the
    people who signed up a week ago, how many have since paid", which necessarily lags by a
    week. This one asks "of the people who arrived in this six-hour slot, how many started a
    trial straight away" — so it reports on today's traffic today, with no lag at all. That
    makes it the fastest read on ad-quality changes available.

    `attach_minutes` is what makes it immediate: 20 of 21 trials in the data start within 15
    minutes of signup (19 within 5), because the paywall sits inside onboarding. A trial
    arriving days later is a different behaviour and is not counted here.

    The cancellation rule is what makes it meaningful. By default ANY cancellation
    disqualifies, whenever it happened: a trial the user eventually kills was not an
    acquisition. Those users stay in the DENOMINATOR — they signed up and did not stick, which
    is the fact being measured.

    CAVEAT WORTH KNOWING: "cancelled" means auto-renew is off, which also catches somebody who
    PAID a full term and then declined the next one. That person is a successful acquisition by
    any reasonable definition, and this metric scores them as a failure. Pass `hold_minutes` to
    restrict disqualification to a window after signup if that distinction starts to matter.
    """
    bounds = era_bounds(eras)
    step = timedelta(hours=bucket_hours)
    buckets = {}
    for u, created in signups:
        if created < earliest:
            continue
        k = earliest + step * int((created - earliest) // step)
        n, conv, counts = buckets.setdefault(k, [0, 0, [0] * len(eras)])
        buckets[k][0] += 1
        starts = trial_starts.get(u) or []
        started = min(starts) if starts else None
        quick = (started is not None
                 and timedelta(0) <= (started - created) <= timedelta(minutes=attach_minutes))
        # `hold_minutes is None` is the default and means ANY cancellation disqualifies,
        # whenever it happened. Set it to relax that to a window after signup.
        bailed = u in cancels and (
            hold_minutes is None
            or (cancels[u] - created) <= timedelta(minutes=hold_minutes))
        if quick and not bailed:
            buckets[k][1] += 1
        buckets[k][2][sum(1 for b in bounds if b <= created)] += 1

    out = []
    for t in sorted(buckets):
        n, k, counts = buckets[t]
        out.append({"t": t, "n": n, "k": k, "era_counts": counts})

    # Trailing pooled rate. At a median of 3 users per six-hour bucket the per-bucket value is
    # 0%, 33% or 50% and carries almost no information; pooling a trailing window is what makes
    # the trend legible without hiding the raw points underneath it.
    win = timedelta(days=trailing_days)
    for i, p in enumerate(out):
        tn = tk = 0
        for q in out[:i + 1]:
            if p["t"] - q["t"] <= win:
                tn += q["n"]; tk += q["k"]
        p["trailing_n"], p["trailing_k"] = tn, tk
    return out


def wilson(k: int, n: int, z: float = Z_95):
    """95% confidence interval for a binomial proportion, as (low, high) fractions.

    Wilson rather than the normal approximation because these series spend their early life at
    n < 10. At n=4, k=0 the normal interval is [0, 0] -- zero width, implying we know the rate
    is exactly zero. Wilson gives roughly [0, 0.49], which is the honest answer and the whole
    reason the band is on the chart.
    """
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _draw_era_ribbon(ax, times, counts):
    """Stacked era-composition ribbon along the top of `ax`. -> legend handles.

    Stacked by composition rather than one flat colour per stretch: after a change the cohort
    genuinely contains both populations for a while, and the proportion is the informative
    part. A single "mixed" band hides whether the new traffic is 5% or 50% of what you are
    looking at, and a short-lived era that never dominates still shows as a visible sliver.

    `get_xaxis_transform()` blends data-space x with axes-fraction y, so the ribbon keeps a
    fixed height at the top no matter what the rate axis is doing.
    """
    from matplotlib.patches import Patch
    handles = []
    n_eras = max((len(c) for c in counts), default=0)
    if not n_eras:
        return handles
    blend = ax.get_xaxis_transform()
    bottom = [RIBBON_BASE] * len(times)
    for idx in range(n_eras):
        share = []
        for c in counts:
            total = sum(c) or 1
            share.append((c[idx] if idx < len(c) else 0) / total)
        top = [bt + s * RIBBON_HEIGHT for bt, s in zip(bottom, share)]
        if any(s > 0 for s in share):
            colour = ERA_COLOURS[idx % len(ERA_COLOURS)]
            ax.fill_between(times, bottom, top, color=colour, alpha=0.85, linewidth=0,
                            transform=blend, zorder=3)
            handles.append(Patch(facecolor=colour, alpha=0.85, label=ERAS[idx][0]))
        bottom = top
    impure = [t for t, c in zip(times, counts) if c and max(c) != sum(c)]
    if impure:
        for edge in (impure[0], impure[-1]):
            ax.axvline(edge, color="#FFFFFF", alpha=0.28, linestyle="--", linewidth=1, zorder=1)
    return handles


def render_rate_page(pdf, data, args, metric: Metric):
    """One page: a rate, its Wilson band, and its denominator on a second axis."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    points = data["points"]
    times = [p["t"] for p in points]
    denoms = [p[metric.denom] for p in points]
    numers = [p[metric.numer] for p in points]
    rates = [(k / n * 100 if n else 0.0) for k, n in zip(numers, denoms)]
    bands = [wilson(k, n) for k, n in zip(numers, denoms)]
    lows = [b[0] * 100 for b in bands]
    highs = [b[1] * 100 for b in bands]

    # The page grows with the data instead of squeezing it, so the x-axis keeps a constant
    # scale no matter how long the series gets. Scroll it horizontally in a PDF viewer.
    span_days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1.0)
    width = max(11.0, span_days * args.inches_per_day)
    fig, ax = plt.subplots(figsize=(width, 7))

    ax.fill_between(times, lows, highs, color=RATE_COLOUR, alpha=0.18, linewidth=0,
                    label="95% confidence (Wilson)")
    ax.plot(times, rates, color=RATE_COLOUR, linewidth=1.8, label=metric.title)

    # Shade the stretches where the denominator is too small to support a reading, rather
    # than marking each point. On page 3 the denominator never exceeds single digits, so
    # per-point markers cover the entire series and bury the line they are annotating.
    # Shading the background says the same thing and leaves the line legible.
    thin = [n < args.min_cohort for n in denoms]
    i = 0
    shaded = False
    while i < len(thin):
        if not thin[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(thin) and thin[j + 1]:
            j += 1
        # A single thin point has zero width as a span; nudge it to one sample wide.
        end = times[j] if j > i else times[min(j + 1, len(times) - 1)]
        ax.axvspan(times[i], end, color=THIN_COLOUR, alpha=0.07, zorder=0)
        shaded = True
        i = j + 1
    if shaded:
        from matplotlib.patches import Patch
        thin_handle = Patch(facecolor=THIN_COLOUR, alpha=0.07,
                            label=f"denominator < {args.min_cohort} — not interpretable")
    else:
        thin_handle = None

    era_handles = _draw_era_ribbon(ax, times, [p.get("era_counts") or [] for p in points])

    ax.set_ylabel("Conversion rate (%)", color=RATE_COLOUR)
    ax.tick_params(axis="y", labelcolor=RATE_COLOUR)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="both", alpha=0.25, linewidth=0.6)

    ax2 = ax.twinx()
    ax2.plot(times, denoms, color=DENOM_COLOUR, linewidth=1.0, linestyle="--",
             label=metric.denom_short)
    ax2.set_ylabel(metric.denom_label, color=DENOM_COLOUR)
    ax2.tick_params(axis="y", labelcolor=DENOM_COLOUR)
    ax2.set_ylim(bottom=0)

    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=6))

    t_last = times[-1]
    ax.annotate(
        f"{rates[-1]:.1f}%   ({numers[-1]}/{denoms[-1]})",
        xy=(t_last, rates[-1]), xytext=(-8, 12), textcoords="offset points",
        ha="right", fontsize=11, fontweight="bold", color=RATE_COLOUR,
    )

    # Three short lines rather than one long one: the figure is only as wide as the series is
    # long, so on a short series a single-line subtitle runs past the right edge and is clipped.
    ax.set_title(
        f"{metric.title} — {data['env']}\n"
        f"{metric.note}\n"
        f"{len(points)} points · every {args.interval_hours}h · "
        f"{times[0]:%Y-%m-%d %H:%M}Z → {t_last:%Y-%m-%d %H:%M}Z",
        fontsize=11, loc="left", pad=12,
    )

    handles, labels = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    if thin_handle is not None:
        handles.append(thin_handle)
        labels.append(thin_handle.get_label())
    for h in era_handles:
        handles.append(h)
        labels.append(h.get_label())
    # Anchored just below the era ribbon rather than at the very top, or it covers the thing
    # it is explaining.
    ax.legend(handles + h2, labels + l2, loc="upper left", bbox_to_anchor=(0.0, 0.94),
              fontsize=9, framealpha=0.9)

    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def render_bucket_page(pdf, data, args):
    """Immediate trial-start rate in fixed signup buckets, with an era ribbon."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    points = data["buckets"]
    if not points:
        return
    times = [p["t"] for p in points]
    ns = [p["n"] for p in points]
    rates = [(p["k"] / p["n"] * 100 if p["n"] else 0.0) for p in points]
    bands = [wilson(p["k"], p["n"]) for p in points]

    span_days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1.0)
    fig, ax = plt.subplots(figsize=(max(11.0, span_days * args.inches_per_day), 7))

    era_handles = _draw_era_ribbon(ax, times, [p.get("era_counts") or [] for p in points])

    ax.fill_between(times, [b[0] * 100 for b in bands], [b[1] * 100 for b in bands],
                    color=RATE_COLOUR, alpha=0.12, linewidth=0,
                    label="95% confidence (Wilson)")
    # Raw buckets as points, not a line. At a median of 3 users each, joining them would draw
    # a jagged line that looks like measured variation when it is mostly 0%, 33%, 50%.
    ax.scatter(times, rates, s=[max(6, min(90, n * 6)) for n in ns],
               color=RATE_COLOUR, alpha=0.55, zorder=4,
               label=f"{args.bucket_hours}h bucket (area = signups)")
    trail = [(p["trailing_k"] / p["trailing_n"] * 100 if p["trailing_n"] else 0.0)
             for p in points]
    ax.plot(times, trail, color=RATE_COLOUR, linewidth=2.0, zorder=5,
            label=f"trailing {args.trailing_days}d pooled")

    hold_txt = ("never cancelled" if args.hold_minutes is None
                else f"not cancelled ≤{args.hold_minutes}min")
    ax.set_ylabel(f"Kept a trial (started ≤{args.attach_minutes}min, {hold_txt}) (%)",
                  color=RATE_COLOUR)
    ax.tick_params(axis="y", labelcolor=RATE_COLOUR)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.25, linewidth=0.6)

    ax2 = ax.twinx()
    ax2.bar(times, ns, width=(args.bucket_hours / 24) * 0.85, color=DENOM_COLOUR,
            alpha=0.22, linewidth=0, zorder=0)
    ax2.set_ylabel(f"Signups per {args.bucket_hours}h", color=DENOM_COLOUR)
    ax2.tick_params(axis="y", labelcolor=DENOM_COLOUR)
    ax2.set_ylim(bottom=0)

    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=6))

    tot_n = sum(ns); tot_k = sum(p["k"] for p in points)
    ax.set_title(
        f"Kept trial-start rate — {data['env']}\n"
        f"bucketed by signup time; counts a trial begun within {args.attach_minutes} min of "
        f"the account AND {hold_txt}\n"
        f"{len(points)} × {args.bucket_hours}h buckets · {tot_k}/{tot_n} overall · "
        f"{times[0]:%Y-%m-%d %H:%M}Z → {times[-1]:%Y-%m-%d %H:%M}Z",
        fontsize=11, loc="left", pad=12)

    h, l = ax.get_legend_handles_labels()
    for p in era_handles:
        h.append(p); l.append(p.get_label())
    ax.legend(h, l, loc="upper left", bbox_to_anchor=(0.0, 0.94), fontsize=9, framealpha=0.9)
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def render_signups_page(pdf, data, args):
    """Raw acquisition volume. No rate, no cohort, no lag — just who arrived and when.

    Worth its own page because every other page here is a ratio, and a ratio cannot tell you
    the difference between "conversion held up" and "almost nobody showed up". A collapse in
    delivery shows here days before it reaches any downstream metric.

    Bars are stacked by era rather than sitting under a separate ribbon: at this scale the bar
    IS the composition, so a second band would repeat it at lower resolution.
    """
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    points = data["buckets"]
    if not points:
        return
    times = [p["t"] for p in points]
    ns = [p["n"] for p in points]
    per_day = 24 / args.bucket_hours

    span_days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1.0)
    fig, ax = plt.subplots(figsize=(max(11.0, span_days * args.inches_per_day), 7))

    width = (args.bucket_hours / 24) * 0.85
    bottom = [0.0] * len(points)
    handles = []
    n_eras = max((len(p.get("era_counts") or []) for p in points), default=0)
    for idx in range(n_eras):
        vals = [(p["era_counts"][idx] if idx < len(p.get("era_counts") or []) else 0)
                for p in points]
        if not any(vals):
            continue
        colour = ERA_COLOURS[idx % len(ERA_COLOURS)]
        ax.bar(times, vals, bottom=bottom, width=width, color=colour, alpha=0.85,
               linewidth=0, zorder=2)
        handles.append(Patch(facecolor=colour, alpha=0.85, label=ERAS[idx][0]))
        bottom = [b + v for b, v in zip(bottom, vals)]

    # Trailing mean over whole buckets, so the line cannot imply resolution the data lacks.
    span = max(1, round(args.trailing_days * 24 / args.bucket_hours))
    trail = [sum(ns[max(0, i - span + 1):i + 1]) / len(ns[max(0, i - span + 1):i + 1])
             for i in range(len(ns))]
    ax.plot(times, trail, color=RATE_COLOUR, linewidth=2.0, zorder=5,
            label=f"trailing {args.trailing_days}d mean")

    ax.set_ylabel(f"New users per {args.bucket_hours}h", color=DENOM_COLOUR)
    ax.tick_params(axis="y", labelcolor=DENOM_COLOUR)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.6)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=6))

    total = sum(ns)
    recent = trail[-1] * per_day if trail else 0
    peak = max(trail) * per_day if trail else 0
    ax.set_title(
        f"New users — {data['env']}\n"
        f"accounts created, bucketed by signup time\n"
        f"{total} total · latest trailing mean {recent:.1f}/day (peak {peak:.1f}) · "
        f"{times[0]:%Y-%m-%d}Z → {times[-1]:%Y-%m-%d}Z",
        fontsize=11, loc="left", pad=12)

    h, l = ax.get_legend_handles_labels()
    for p in handles:
        h.append(p); l.append(p.get_label())
    ax.legend(h, l, loc="upper left", fontsize=9, framealpha=0.9)
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


# One page per metric, then the immediate-rate page, then raw volume.
PAGES = ([partial(render_rate_page, metric=m) for m in METRICS]
         + [render_bucket_page, render_signups_page])


def build_pdf(data, out_path: Path, args) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_pdf import PdfPages

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out_path) as pdf:
        for render in PAGES:
            render(pdf, data, args)
    return out_path


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="production", choices=["production", "staging"])
    parser.add_argument("--earliest", default=DEFAULT_EARLIEST,
                        help=f"Earliest signup data of interest, ISO-8601 UTC. "
                             f"The first evaluable point is {COHORT_START_DAYS}d after it. "
                             f"Default: {DEFAULT_EARLIEST}")
    parser.add_argument("--interval-hours", type=int, default=1,
                        help="Sampling interval. Default: 1 (hourly).")
    parser.add_argument("--max-lookback-days", type=int, default=180,
                        help="Never plot further back than this. Default: 180.")
    parser.add_argument("--inches-per-day", type=float, default=1.5,
                        help="Page width per day of series. Lower this rather than shortening "
                             "the window if the page gets unwieldy. Default: 1.5")
    parser.add_argument("--bucket-hours", type=int, default=24,
                        help="Bucket width for the immediate trial-start page. Default: 24. "
                             "At current volume a 6h bucket holds a median of 3 signups, so "
                             "its rate is 0%%/33%%/50%% noise; 24h gives a median of 10. Drop "
                             "it once daily signups are high enough to support it.")
    parser.add_argument("--attach-minutes", type=int, default=15,
                        help="A trial counts as immediate if it starts within this many "
                             "minutes of the account. Default: 15 (captures 20 of 21).")
    parser.add_argument("--hold-minutes", type=int, default=None,
                        help="If set, only a cancellation within this many minutes of signup "
                             "disqualifies a trial. Default: unset — ANY cancellation "
                             "disqualifies, whenever it happened.")
    parser.add_argument("--trailing-days", type=int, default=7,
                        help="Pooled trailing window drawn over the raw buckets. Default: 7.")
    parser.add_argument("--min-cohort", type=int, default=10,
                        help="Points whose denominator is smaller than this are marked as "
                             "uninterpretable. Default: 10")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output PDF path (default: plots/conversion_trend_<env>.pdf).")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not open the PDF when finished.")
    args = parser.parse_args()

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("ERROR: matplotlib is required. Install it with:\n  pip install matplotlib",
              file=sys.stderr)
        return 1

    earliest = parse_dt(args.earliest)
    if earliest is None:
        print(f"Could not parse --earliest {args.earliest!r}; expected ISO-8601 "
              f"(e.g. 2026-09-06T02:33:00).", file=sys.stderr)
        return 1
    if args.interval_hours < 1:
        print("--interval-hours must be at least 1.", file=sys.stderr)
        return 1

    try:
        dynamodb = boto3.Session(region_name=REGION).resource("dynamodb")
        print(f"Scanning {args.env} users and entitlement-grants...")
        users = scan_users(dynamodb, args.env, TEST_ACCOUNT_SUBSTRINGS)
        signups = [
            (u["userId"], u["created"]) for u in users
            if u["created"] is not None
            and (u["email"] or "").lower() not in NON_CUSTOMER_EMAILS
        ]
        trial_starts, paid_starts = scan_grants(dynamodb, args.env)
        cancels = scan_cancellations(dynamodb, args.env)
    except NoCredentialsError:
        print("ERROR: no AWS credentials found.", file=sys.stderr)
        return 1

    # Naive UTC, to match what `parse_dt` returns for every stored timestamp. Mixing a
    # tz-aware "now" with naive grant/signup times raises on the first comparison.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    points = build_series(signups, trial_starts, paid_starts, earliest, now,
                          args.interval_hours, args.max_lookback_days, eras=ERAS)

    if not points:
        first = earliest + timedelta(days=COHORT_START_DAYS)
        print(f"Nothing to plot: the first evaluable instant is {first:%Y-%m-%d %H:%M}Z, "
              f"which is still in the future.", file=sys.stderr)
        return 1

    last = points[-1]
    print(f"  {len(signups)} users, "
          f"{sum(len(v) for v in trial_starts.values())} trial grants, "
          f"{sum(len(v) for v in paid_starts.values())} yearly paid grants")
    print(f"  {len(points)} points, "
          f"{points[0]['t']:%Y-%m-%d %H:%M}Z -> {last['t']:%Y-%m-%d %H:%M}Z")
    print(f"  cohort size {min(p['cohort'] for p in points)}"
          f"..{max(p['cohort'] for p in points)}")
    for m in METRICS:
        n, k = last[m.denom], last[m.numer]
        if n:
            lo, hi = wilson(k, n)
            print(f"  {m.title:<40} {k}/{n} = {k / n * 100:>5.1f}%  "
                  f"(95% CI {lo * 100:.1f}–{hi * 100:.1f}%)")
        else:
            print(f"  {m.title:<40} denominator is empty")

    print("  cohort composition by era (share of the measurement window):")
    for label, first, peak, peak_at, enters in era_summary(points, ERAS, now):
        if first is None:
            when = (f"enters the window {enters:%Y-%m-%d %H:%M}Z"
                    if enters and enters > now else "no signups recorded")
            print(f"    {label:<24} not yet measurable — {when}")
        else:
            print(f"    {label:<24} from {first:%m-%d %H:%M}  peak {peak * 100:>3.0f}% "
                  f"of cohort at {peak_at:%m-%d %H:%M}")

    buckets = build_bucket_series(signups, trial_starts, cancels, earliest, now,
                                  args.bucket_hours, args.attach_minutes, args.hold_minutes,
                                  args.trailing_days, ERAS)
    if buckets:
        bn = sum(b["n"] for b in buckets); bk = sum(b["k"] for b in buckets)
        thin = sum(1 for b in buckets if b["n"] < 5)
        hold = ("never cancelled" if args.hold_minutes is None
                else f"held >{args.hold_minutes}min")
        print(f"  kept trial-start (≤{args.attach_minutes}min, {hold}): "
              f"{bk}/{bn} = {bk / bn * 100:.1f}% over {len(buckets)} × {args.bucket_hours}h buckets")
        if thin:
            print(f"    {thin}/{len(buckets)} buckets have <5 signups — per-bucket rates there "
                  f"are noise; read the trailing line.")

    out_path = args.out or (OUTPUT_DIR / f"conversion_trend_{args.env}.pdf")
    out = build_pdf({"points": points, "buckets": buckets, "env": args.env}, out_path, args)
    print(f"Done -> {out}  ({len(PAGES)} pages)")

    if not args.no_open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, str(out)], check=False)
        except FileNotFoundError:
            print(f"(Could not auto-open; open it manually: {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
