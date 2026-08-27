#!/usr/bin/env python3
"""Plot one production user's lift sets per exercise, weight over time, coloured by effort.

READ-ONLY against DynamoDB: `get_item` and `query` only, never `scan`, never a write. The
only thing written to disk is the output PDF.

What it does
------------
One panel per exercise the user has ever logged a set for:

    x = when the set was logged
    y = the weight lifted
    colour = the effort band that set represented AT THE TIME, derived from the
             estimated-1RM standing immediately before it

Sibling of `plot_user_lift_timelines.py`, which sweeps every user and whose per-exercise pages
plot % of e1RM. That axis makes colour and height say the same thing twice. Putting weight on
y instead buys a second, independent fact per dot: height is the load actually moved, colour is
what that load cost relative to the ceiling on the day. A 225 that was near-max in March and
easy in August is the entire point, and it is invisible on a percentage axis.

Every exercise appears, accessories included, with no minimum set count -- so a user with a
long catalogue produces a long document. Fundamentals lead, then accessories by descending
set count.

Usage
-----
    workon liftthebull-backend
    python scripts/plot_user_weight_by_exercise.py --user-id <uuid>
    python scripts/plot_user_weight_by_exercise.py --user-id <uuid> --env staging
    python scripts/plot_user_weight_by_exercise.py --user-id <uuid> --no-open

Output defaults to  WeightApp-backend/plots/user_weight_by_exercise_<env>_<userid8>.pdf
"""

import argparse
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, NoCredentialsError

# Running `python scripts/thing.py` puts `scripts/` on sys.path[0] and NOT the repo root, so
# the sibling import below fails without this even though `scripts/__init__.py` exists.
# Verified: bare `from scripts.plot_user_lift_timelines import ...` raises ModuleNotFoundError.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.plot_user_lift_timelines import (  # noqa: E402
    EFFORT_COLORS,
    OUTPUT_DIR,
    REGION,
    TIER_COLOR,
    TIER_TITLE,
    _attach_effort_data,
    _draw_now,
    _effort_legend,
    _label_for,
    _truncate,
    parse_dt,
    query_estimated_1rm,
    query_lift_sets,
    table_name,
)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

PER_PAGE = 4  # 2 x 2

# Widest subtitle a half-width panel can hold at 10pt bold before it reaches its neighbour.
SUBTITLE_MAX = 66

# Palette, matching the sibling so the two documents read as one family.
INK = "#102a43"
SUB = "#627d98"
DIM = "#9fb3c8"
RULE = "#cbd5e0"

# Reps panels get a faint warm wash. Scanning a 2x2 grid, nothing else distinguishes a panel
# whose y-axis is reps from one whose y-axis is pounds, and misreading that is worse than the
# tint is ugly.
REPS_PANEL_BG = "#FFFBF2"


# --------------------------------------------------------------------------- #
# DynamoDB (read-only)
# --------------------------------------------------------------------------- #
def get_user(dynamodb, env: str, user_id: str):
    """users.get_item -> {userId, email, name, created}, or None if there is no such row.

    Shaped exactly like the dicts `scan_users` produces in the sibling, so `_label_for` works
    on it unmodified.
    """
    table = dynamodb.Table(table_name(env, "users"))
    resp = table.get_item(
        Key={"userId": user_id},
        # "name" is a DynamoDB reserved word and has to be aliased. "fullName" (Apple Sign-In)
        # and "name" (email/password register) are the two places a display name can live.
        ProjectionExpression="userId, emailAddress, createdDatetime, fullName, #nm",
        ExpressionAttributeNames={"#nm": "name"},
    )
    item = resp.get("Item")
    if not item:
        return None
    return {
        "userId": item["userId"],
        "email": item.get("emailAddress", ""),
        "name": (item.get("fullName") or item.get("name") or "").strip(),
        "created": parse_dt(item.get("createdDatetime")),
    }


def get_user_properties(dynamodb, env: str, user_id: str):
    """user-properties.get_item -> {bodyweight, sex, tier, version, unit}. Never raises.

    Only bodyweight and sex affect the drawing (they feed the tier math in
    `_attach_effort_data`); the rest is header material. A missing item is normal for an
    account that never finished onboarding, so every field independently degrades to None.
    """
    table = dynamodb.Table(table_name(env, "user-properties"))
    resp = table.get_item(
        Key={"userId": user_id},
        ProjectionExpression="bodyweight, biologicalSex, hasMetStrengthTierConditions, "
                             "latestAppVersion, weightUnit",
    )
    item = resp.get("Item") or {}
    bw = item.get("bodyweight")
    sex = item.get("biologicalSex")
    unit = item.get("weightUnit")
    return {
        "bodyweight": float(bw) if bw is not None else None,
        "sex": sex.lower() if isinstance(sex, str) else None,
        "tier": item.get("hasMetStrengthTierConditions") is True,
        "version": item.get("latestAppVersion") or None,
        "unit": unit.lower() if isinstance(unit, str) else None,
    }


def query_exercises_named(dynamodb, env: str, user_id: str):
    """exercises -> {exerciseItemId: {name, loadType, deleted}}, KEEPING deleted rows.

    Deliberately NOT `plot_user_lift_timelines.query_exercises`, which skips `deleted` rows.
    That is right for the sibling and wrong here: lift-sets outlive the exercise they belong
    to, and this script draws a panel for every exercise with a single set. Dropping deleted
    rows would title several distinct panels "Unknown" with no way to tell them apart.
    """
    table = dynamodb.Table(table_name(env, "exercises"))
    by_id = {}
    query_kwargs = {"KeyConditionExpression": Key("userId").eq(user_id)}
    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get("Items", []):
            by_id[item.get("exerciseItemId", "")] = {
                "name": item.get("name", "Unknown"),
                "loadType": item.get("loadType", ""),
                "deleted": item.get("deleted") is True,
            }
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return by_id


# --------------------------------------------------------------------------- #
# Panel preparation
# --------------------------------------------------------------------------- #
def _has_load(s) -> bool:
    return s.get("weight") is not None and s["weight"] > 0


# Share of a panel's sets that must carry added load for a weight axis to be worth drawing.
BODYWEIGHT_PANEL_SHARE = 0.4


def _panel_mode(ex) -> str:
    """"weight" or "reps", decided by how much of the panel actually carries load.

    For Single Load and Bodyweight + Single Load exercises the stored weight is the ADDED
    load, so 0 is a real value meaning "bodyweight only" rather than missing data. An exercise
    that is entirely bodyweight draws as a flat line along y=0 -- true, and useless.

    The threshold is a share rather than "any set has load" because the majority case is worse
    than the pure one: a Pull Ups panel with sixteen bodyweight sets and one at +10lb rendered
    as sixteen dots on the floor and a lone outlier, which is a chart about the exception
    instead of the work. Below 40% loaded, reps is the axis where something actually varies.
    """
    if not ex["sets"]:
        return "weight"
    loaded = sum(1 for s in ex["sets"] if _has_load(s))
    return "weight" if loaded / len(ex["sets"]) >= BODYWEIGHT_PANEL_SHARE else "reps"


def _resolve_name(ex, ex_meta) -> str:
    """Panel title, recovering deleted and orphaned exercises rather than collapsing them.

    `_attach_effort_data` resolves fundamentals from their deterministic ids and everything
    else from the exercises table, but it does not keep the exercise id, so recover it from
    the first set. Orphans keep their id fragment so two of them stay distinguishable.
    """
    if ex["is_fundamental"]:
        return ex["name"]

    ex_id = (ex["sets"][0].get("exerciseId") or "") if ex["sets"] else ""
    meta = ex_meta.get(ex_id)
    if meta:
        return f"{meta['name']} (deleted)" if meta.get("deleted") else meta["name"]
    if not ex_id:
        return "(no exercise id)"
    return f"Unknown · {ex_id[:8]}…"


def _order_exercises(exercises):
    """Fundamentals in canonical order, then accessories by descending set count.

    Same policy as the sibling's sort, with `name` added as a final tiebreak: equal-count
    accessories otherwise fall back to DynamoDB return order, which is not stable between
    runs, so two renders of the same user could shuffle panels between pages.
    """
    fund_order = ["Deadlifts", "Squats", "Bench Press", "Overhead Press", "Barbell Rows"]

    def key(ex):
        if ex["is_fundamental"] and ex["fund_name"] in fund_order:
            return (0, fund_order.index(ex["fund_name"]), "")
        return (1, -ex["count"], ex["title"].lower())

    return sorted(exercises, key=key)


def _prepare_panels(u, ex_meta):
    """Resolve titles, choose each panel's y-axis, and attach the y value to every set.

    All mutation happens here so `_weight_subplot` is only ever about drawing. Attaching onto
    the set dicts follows the precedent of `classify_exercise_sets`, which already appends
    `epley` / `bucket` / `pct` to these same dicts.
    """
    panels = []
    dropped = 0
    for ex in u.get("effort_exercises", []):
        ex["title"] = _resolve_name(ex, ex_meta)
        ex["mode"] = _panel_mode(ex)

        plot_sets = []
        for s in ex["sets"]:
            if ex["mode"] == "weight":
                # `weight` is required by the schema, so None means a malformed row. There is
                # no honest y for it, so drop the point and report the count rather than
                # silently placing it at zero among the real bodyweight-only sets.
                if s.get("weight") is None:
                    dropped += 1
                    continue
                s["y"] = float(s["weight"])
            else:
                if s.get("reps") is None:
                    dropped += 1
                    continue
                s["y"] = float(s["reps"])
            plot_sets.append(s)

        if not plot_sets:
            continue
        ex["plot_sets"] = plot_sets
        ex["best"] = max(s["y"] for s in plot_sets)
        ex["zero_count"] = sum(1 for s in plot_sets if not _has_load(s))
        panels.append(ex)

    return _order_exercises(panels), dropped


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _pr_annotations(plot_sets, ax, mdates, max_labels=8, min_sep=0.06):
    """Which progress sets get a "weight x reps" label, and none close enough to collide.

    Capped and spaced, unlike the sibling. On its % axis every PR pins near the top of the
    panel and they spread out horizontally; on a weight axis PRs climb diagonally and bunch
    into early history, where nearly every set is a PR. Labelling all of them produced
    "185x8185x7" -- two labels overprinted into nonsense.

    So: walk PRs heaviest-first (the most interesting ones win ties for space) and keep one
    only if it clears every already-kept label by `min_sep` in AXES-FRACTION space. Normalising
    is the point -- separation has to be judged in the panel the reader sees, not in pounds and
    days, which have no common scale. Must be called AFTER the limits are final.
    """
    prs = [s for s in plot_sets if s.get("bucket") == "progress"]
    if not prs:
        return []

    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    xspan = (x1 - x0) or 1.0
    yspan = (y1 - y0) or 1.0

    def norm(s):
        return ((mdates.date2num(s["date"]) - x0) / xspan, (s["y"] - y0) / yspan)

    # Most recent first so the latest PR -- the one a reader looks for -- always gets a label,
    # then the heaviest fill in whatever room is left.
    ordered = [prs[-1]] + sorted(prs[:-1], key=lambda s: s.get("epley") or 0, reverse=True)

    kept, spots = [], []
    for s in ordered:
        if len(kept) >= max_labels:
            break
        px, py = norm(s)
        if any(abs(px - qx) < min_sep and abs(py - qy) < min_sep for qx, qy in spots):
            continue
        spots.append((px, py))
        kept.append(s)
    return kept


def _weight_subplot(ax, ex, now, mdates):
    """One exercise: weight (or reps) over time, coloured by the effort band of each set."""
    plot_sets = ex["plot_sets"]
    reps_mode = ex["mode"] == "reps"

    if reps_mode:
        ax.set_facecolor(REPS_PANEL_BG)

    for s in plot_sets:
        band = s.get("bucket") or "easy"
        reps = s.get("reps") or 0
        # Identical size formula to the sibling, so the two documents are cross-readable.
        # Size earns its place more here than there: on this axis 300x3 and 300x8 land on the
        # same height, so marker area is the only place rep count exists.
        size = 24 + min(reps, 20) * 7
        bodyweight_only = not _has_load(s)
        ax.scatter(
            s["date"], s["y"], s=size,
            marker=("D" if s.get("baseline") else "o"),
            facecolor=("none" if (bodyweight_only and not reps_mode) else EFFORT_COLORS[band]),
            edgecolor=EFFORT_COLORS[band],
            linewidths=(1.3 if (bodyweight_only and not reps_mode) else 0.5),
            alpha=0.85, zorder=3,
        )

    # A mixed panel has real sets sitting at y=0. Without a rule there they read as an axis
    # artefact rather than as "bodyweight only, no plates".
    if not reps_mode and ex["zero_count"]:
        ax.axhline(0, color=RULE, lw=0.8, ls=(0, (3, 3)), zorder=1)

    lo = min(s["y"] for s in plot_sets)
    hi = ex["best"]
    pad = max((hi - lo) * 0.12, hi * 0.05, 5.0)
    ax.set_ylim(min(lo - pad, -pad * 0.5) if not reps_mode else max(0, lo - pad), hi + pad)

    # A single-set panel has a zero-width x range, and `_draw_now` only ever moves the RIGHT
    # edge -- so without this the lone point is welded to the left spine.
    dates = [s["date"] for s in plot_sets]
    is_single_point = len(dates) == 1 or dates[0] == dates[-1]
    if is_single_point:
        ax.set_xlim(mdates.date2num(dates[0]) - 3, mdates.date2num(dates[0]) + 3)

    ax.set_ylabel("Reps (no added load)" if reps_mode else "Weight (lb)", fontsize=9)
    ax.tick_params(labelsize=8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.grid(axis="y", color="#eef2f6", linewidth=0.8)
    ax.set_axisbelow(True)

    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

    # The Today line costs horizontal space, because `_draw_now` stretches xlim out to the
    # present. That is free on a currently-active lift and ruinous on a dormant one: a panel
    # whose sets stop in April, rendered in August, squeezed five months of training into the
    # left quarter and gave the majority of the panel to empty space.
    #
    # So draw it only when the gap is small relative to the history being shown. When it is
    # not, clamp to the data and put the staleness in the subtitle instead, where it costs no
    # pixels and is easier to read than a line position anyway.
    stale_days = 0
    if not is_single_point:
        span_days = max((dates[-1] - dates[0]).days, 1)
        gap_days = (now - dates[-1]).days
        if gap_days <= max(span_days * 0.35, 21):
            _draw_now(ax, now, mdates)
        else:
            stale_days = gap_days
            pad_days = max(span_days * 0.04, 1)
            ax.set_xlim(mdates.date2num(dates[0]) - pad_days,
                        mdates.date2num(dates[-1]) + pad_days)
    else:
        _draw_now(ax, now, mdates)

    # After the limits are settled, so separation can be judged in the panel as drawn.
    for s in _pr_annotations(plot_sets, ax, mdates):
        w, r = s.get("weight"), s.get("reps")
        if w is None or r is None:
            continue
        ax.annotate(f"{int(w)}×{r}", (s["date"], s["y"]), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=6.0, color="#334e68", zorder=4)

    if reps_mode:
        # "colour ≈ reps" is stated because it is a real limitation, not a feature: for
        # zero-weight sets the effort band is derived from rep count, so here colour is a
        # function of height rather than the independent fact it is on a weight panel.
        loaded = sum(1 for s in plot_sets if _has_load(s))
        extra = f" · {loaded} loaded" if loaded else ""
        subtitle = f"bodyweight · {ex['count']} sets{extra} · colour ≈ reps"
    else:
        tier = f" · {TIER_TITLE.get(ex['tier'], '')}" if ex.get("tier") else ""
        subtitle = f"best {int(ex['best'])} lb · e1RM {int(ex['current_e1rm'])} lb · {ex['count']} sets{tier}"
    if stale_days:
        subtitle += f" · {stale_days}d idle"
    # Hard-capped, and the phrasing above kept terse, because a 2x2 grid gives each subtitle
    # only half the figure width: an over-long one runs straight into the neighbouring panel's
    # title, which is how "colour follows reps · last logged 152d ago" read as one sentence
    # continuing into "Weighted Dips".
    ax.set_title(f"{_truncate(ex['title'], 34)}\n{_truncate(subtitle, SUBTITLE_MAX)}",
                 fontsize=10, fontweight="bold", color=INK, loc="left", pad=8)


def _encoding_note(fig):
    """One line explaining the channels the legend does not cover."""
    fig.text(0.5, 0.862,
             "y = weight lifted (lb) · colour = effort at time of set · marker size = reps"
             "  ·  ◆ baseline set  ·  ○ bodyweight only",
             ha="center", va="top", fontsize=7.5, color=SUB)


def _page_header(fig, u, panels, pidx, n_pages, env, unit_note):
    total_sets = len(u["sets"])
    first, last = u["sets"][0]["date"], u["sets"][-1]["date"]
    fig.text(0.07, 0.965, "Weight Lifted by Exercise", ha="left", va="top",
             fontsize=16, fontweight="bold", color=INK)
    fig.text(0.07, 0.935,
             f"{_truncate(_label_for(u), 34)} · {u['userId'][:8]}… · {total_sets} sets · "
             f"{len(panels)} exercises · {first:%Y-%m-%d} → {last:%Y-%m-%d}",
             ha="left", va="top", fontsize=9.5, color=SUB)

    tier = u.get("overall_tier", "none")
    fig.text(0.965, 0.965, f"★ {TIER_TITLE.get(tier, 'None')}", ha="right", va="top",
             fontsize=14, fontweight="bold", color=TIER_COLOR.get(tier, DIM))
    bw_note = " (default)" if u.get("bw_defaulted") else ""
    sex_note = " (default)" if u.get("sex_defaulted") else ""
    fig.text(0.965, 0.936,
             f"BW {int(u.get('bodyweight_used', 0))} lb{bw_note} · "
             f"{u.get('sex_used', '?')}{sex_note} · {env}",
             ha="right", va="top", fontsize=8.5, color=SUB)
    tail = f"page {pidx + 1}/{n_pages}" + (f" · {unit_note}" if unit_note else "")
    fig.text(0.965, 0.914, tail, ha="right", va="top", fontsize=8, color=DIM)


def build_pdf(u, panels, out_path: Path, env: str, unit_note: str = "") -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.lines import Line2D

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # One `now` for the whole document so every page's Today line sits at the same place.
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    chunks = [panels[i:i + PER_PAGE] for i in range(0, len(panels), PER_PAGE)]
    with PdfPages(out_path) as pdf:
        for pidx, chunk in enumerate(chunks):
            fig, axes = plt.subplots(2, 2, figsize=(13, 9.5))
            axes = axes.flatten()
            fig.subplots_adjust(left=0.07, right=0.965, top=0.80, bottom=0.07,
                                hspace=0.46, wspace=0.18)
            _page_header(fig, u, panels, pidx, len(chunks), env, unit_note)
            _effort_legend(fig, Line2D)
            _encoding_note(fig)
            for ax, ex in zip(axes, chunk):
                _weight_subplot(ax, ex, now, mdates)
            for ax in axes[len(chunk):]:
                ax.axis("off")
            pdf.savefig(fig)
            plt.close(fig)
    return out_path


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True, help="User UUID to plot.")
    parser.add_argument("--env", default="production", choices=["production", "staging"])
    parser.add_argument("--out", type=Path, default=None,
                        help="Output PDF path (default: plots/user_weight_by_exercise_<env>_<id8>.pdf).")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not open the PDF automatically when done.")
    args = parser.parse_args()

    # Validated before any AWS call, so a typo costs nothing.
    if not UUID_RE.match(args.user_id):
        print(f"ERROR: --user-id must be a UUID, got {args.user_id!r}", file=sys.stderr)
        return 2

    dynamodb = boto3.resource("dynamodb", region_name=REGION)

    try:
        user = get_user(dynamodb, args.env, args.user_id)
        sets = query_lift_sets(dynamodb, args.env, args.user_id)
    except NoCredentialsError:
        print("ERROR: No AWS credentials found. Configure your profile as you do for "
              "`make save-user-production`.", file=sys.stderr)
        return 1
    except ClientError as e:
        print(f"ERROR reading {args.env}: {e}", file=sys.stderr)
        return 1

    if user is None:
        # Not fatal on its own -- the users row only feeds the header, and the sets are the
        # actual subject. Fatal only when there is nothing to draw either.
        print(f"  WARN: no users row for {args.user_id} in {args.env}; header will be sparse.",
              file=sys.stderr)
        user = {"userId": args.user_id, "email": "", "name": "", "created": None}

    if not sets:
        print(f"No lift-sets for {args.user_id} in {args.env}. Nothing to plot.", file=sys.stderr)
        return 1
    print(f"  {len(sets)} lift-sets.")

    props = get_user_properties(dynamodb, args.env, args.user_id)
    if props["bodyweight"] is None or props["sex"] is None:
        print("  WARN: user-properties missing bodyweight and/or sex; tier math will use "
              "the app's defaults (200 lb / male).", file=sys.stderr)

    try:
        e1rm_by_set = query_estimated_1rm(dynamodb, args.env, args.user_id)
        ex_meta = query_exercises_named(dynamodb, args.env, args.user_id)
    except ClientError as e:
        # Degrade rather than abort: without these, names fall back to Unknown and effort is
        # classified from Epley alone. Still a useful chart.
        print(f"  WARN: could not read exercises/estimated-1rm ({e}); continuing degraded.",
              file=sys.stderr)
        e1rm_by_set, ex_meta = {}, {}

    user.update({"sets": sets, "bodyweight": props["bodyweight"], "sex": props["sex"]})
    # min_sets=1: every exercise gets a panel, including one-off accessories.
    _attach_effort_data(user, e1rm_by_set, ex_meta, min_sets=1)

    panels, dropped = _prepare_panels(user, ex_meta)
    if dropped:
        print(f"  WARN: {dropped} set(s) had no usable weight/reps and were not plotted.",
              file=sys.stderr)
    if not panels:
        print("No exercises resolved to a plottable panel.", file=sys.stderr)
        return 1

    reps_panels = sum(1 for p in panels if p["mode"] == "reps")
    print(f"  {len(panels)} exercises ({reps_panels} bodyweight-only) "
          f"-> {math.ceil(len(panels) / PER_PAGE)} pages.")

    unit_note = "user displays kg" if props.get("unit") == "kg" else ""
    out = args.out or (OUTPUT_DIR / f"user_weight_by_exercise_{args.env}_{args.user_id[:8]}.pdf")
    build_pdf(user, panels, out, args.env, unit_note)
    print(f"Wrote {out}")

    if not args.no_open:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
