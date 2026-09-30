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
# fully in effect until 14. Between those two the cohort straddles the boundary and belongs to
# neither era. `era_zones` does that translation; shading on the raw boundary would credit the
# new era with a week of the old one's users.
ERAS = [
    ("Ads $30/day", None),
    ("Ads $60/day", "2026-09-23T21:31:51.108"),
]

RATE_COLOUR = "#4C78A8"
DENOM_COLOUR = "#8C8C8C"
THIN_COLOUR = "#C0392B"
# Ribbon colours, one per era in order. Muted on purpose: this is context behind the data,
# not a second data series competing with the line.
ERA_COLOURS = ["#5B8C5A", "#8E6C9B", "#C08B4A", "#4A7C94"]
MIXED_COLOUR = "#9E9E9E"


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


def build_series(signups, trial_starts, paid_starts, earliest, now,
                 interval_hours, max_lookback_days):
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
        points.append({"t": t, "cohort": len(cohort), "trial": trial,
                       "paid": paid, "trial_paid": trial_paid})
        t += step
    return points


def era_zones(points, eras):
    """-> [(start_t, end_t, label_or_None)] over the sampled instants, None meaning mixed.

    A point is attributed to an era only when its ENTIRE cohort window falls inside that era.
    If any boundary lands strictly inside [t-14d, t-7d) the cohort is part old and part new,
    and the honest label is neither. Those stretches are exactly one COHORT_START_DAYS minus
    COHORT_END_DAYS wide -- a 7-day blind spot after every boundary, which is a property of
    the metric and not something the plot can smooth away.
    """
    bounds = [parse_dt(s) for _, s in eras[1:]]
    bounds = [b for b in bounds if b is not None]
    labels = [name for name, _ in eras]

    def classify(t):
        lo = t - timedelta(days=COHORT_START_DAYS)
        hi = t - timedelta(days=COHORT_END_DAYS)
        if any(lo < b < hi for b in bounds):
            return None
        idx = sum(1 for b in bounds if b <= lo)
        return labels[min(idx, len(labels) - 1)]

    runs = []
    for p in points:
        label = classify(p["t"])
        if runs and runs[-1][2] == label:
            runs[-1][1] = p["t"]
        else:
            runs.append([p["t"], p["t"], label])
    return [(a, b, c) for a, b, c in runs]


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

    # Era ribbon along the very top, plus a dashed rule at each zone change. A ribbon rather
    # than a full-height tint because the background is already carrying the thin-denominator
    # shading, and two overlapping washes stop reading as either one.
    era_handles = []
    seen = set()
    for start, end, label in data.get("zones", []):
        colour = MIXED_COLOUR if label is None else ERA_COLOURS[
            [n for n, _ in ERAS].index(label) % len(ERA_COLOURS)]
        ax.axvspan(start, end, ymin=0.955, ymax=1.0, color=colour, alpha=0.75, linewidth=0)
        key = label or "__mixed__"
        if key not in seen:
            seen.add(key)
            from matplotlib.patches import Patch
            era_handles.append(Patch(
                facecolor=colour, alpha=0.75,
                label=label or f"mixed cohort ({COHORT_START_DAYS - COHORT_END_DAYS}d after a change)"))
    for start, _, _ in data.get("zones", [])[1:]:
        ax.axvline(start, color="#FFFFFF", alpha=0.28, linestyle="--", linewidth=1, zorder=1)

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


# One page per metric, in order. Append further pages here; each takes (pdf, data, args).
PAGES = [partial(render_rate_page, metric=m) for m in METRICS]


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
    except NoCredentialsError:
        print("ERROR: no AWS credentials found.", file=sys.stderr)
        return 1

    # Naive UTC, to match what `parse_dt` returns for every stored timestamp. Mixing a
    # tz-aware "now" with naive grant/signup times raises on the first comparison.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    points = build_series(signups, trial_starts, paid_starts, earliest, now,
                          args.interval_hours, args.max_lookback_days)

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

    zones = era_zones(points, ERAS)
    print("  eras (by evaluation instant, cohort fully inside unless marked mixed):")
    for start, end, label in zones:
        days = (end - start).total_seconds() / 86400
        print(f"    {start:%m-%d %H:%M} -> {end:%m-%d %H:%M}  ({days:>5.2f}d)  "
              f"{label or 'MIXED — cohort straddles a change'}")

    out_path = args.out or (OUTPUT_DIR / f"conversion_trend_{args.env}.pdf")
    out = build_pdf({"points": points, "env": args.env, "zones": zones}, out_path, args)
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
