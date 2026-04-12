#!/usr/bin/env python3
"""Capture a point-in-time system health snapshot for LLM analysis.

Collects CloudWatch metrics, DynamoDB table stats, and user counts
across all backend services. Snapshots are stored as JSON and can be
compared over time to identify growth trends and anomalies.

Usage:
    python scripts/system_snapshot.py                    # production, last 24h
    python scripts/system_snapshot.py --env staging      # staging
    python scripts/system_snapshot.py --hours 168        # last 7 days
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3

# ─── Configuration ─────────────────────────────────────────────────────────────

REGION = "us-west-1"
PROJECT = "liftthebull"

SNAPSHOT_DIR = Path(__file__).parent.parent / "system-snapshots"

LAMBDA_FUNCTIONS = [
    "auth",
    "checkin",
    "user",
    "entitlements",
    "insights",
    "email-processing",
]

DYNAMO_TABLES = [
    "users",
    "user-properties",
    "exercises",
    "lift-sets",
    "estimated-1rm",
    "set-plans",
    "accessory-goal-checkins",
    "groups",
    "entitlement-grants",
    "subscription-events",
    "insight-tasks",
    "insights-cache",
]

# ─── Helpers ───────────────────────────────────────────────────────────────────

class SnapshotEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def cw_sum(cw, namespace, metric, dimensions, start, end, period):
    """Get sum of a CloudWatch metric over a time range."""
    resp = cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Sum"],
    )
    return sum(dp["Sum"] for dp in resp.get("Datapoints", []))


def cw_stats(cw, namespace, metric, dimensions, start, end, period, statistics):
    """Get multiple statistics for a CloudWatch metric."""
    resp = cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=statistics,
    )
    if not resp.get("Datapoints"):
        return {s: 0 for s in statistics}
    # Aggregate: Sum for Sum, max of Max, avg of Average
    result = {}
    for stat in statistics:
        vals = [dp[stat] for dp in resp["Datapoints"] if stat in dp]
        if not vals:
            result[stat] = 0
        elif stat == "Sum":
            result[stat] = sum(vals)
        elif stat == "Maximum":
            result[stat] = max(vals)
        elif stat == "Average":
            result[stat] = sum(vals) / len(vals)
    return result


def cw_percentile(cw, namespace, metric, dimensions, start, end, period, percentile):
    """Get a percentile statistic via extended statistics."""
    resp = cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=start,
        EndTime=end,
        Period=period,
        ExtendedStatistics=[percentile],
    )
    vals = [dp["ExtendedStatistics"].get(percentile, 0) for dp in resp.get("Datapoints", []) if "ExtendedStatistics" in dp]
    return max(vals) if vals else 0


# ─── Collectors ────────────────────────────────────────────────────────────────

def collect_api_gateway(cw, env, start, end, period):
    """Collect API Gateway metrics."""
    api_name = f"{PROJECT}-{env}-api"
    dims = {"ApiName": api_name}

    latency_p50 = cw_percentile(cw, "AWS/ApiGateway", "Latency", dims, start, end, period, "p50")
    latency_p90 = cw_percentile(cw, "AWS/ApiGateway", "Latency", dims, start, end, period, "p90")
    latency_p99 = cw_percentile(cw, "AWS/ApiGateway", "Latency", dims, start, end, period, "p99")

    return {
        "total_requests": int(cw_sum(cw, "AWS/ApiGateway", "Count", dims, start, end, period)),
        "4xx_errors": int(cw_sum(cw, "AWS/ApiGateway", "4XXError", dims, start, end, period)),
        "5xx_errors": int(cw_sum(cw, "AWS/ApiGateway", "5XXError", dims, start, end, period)),
        "latency_ms": {
            "p50": round(latency_p50, 1),
            "p90": round(latency_p90, 1),
            "p99": round(latency_p99, 1),
        },
    }


def collect_lambda(cw, env, start, end, period):
    """Collect Lambda metrics for all functions."""
    results = {}
    for fn_suffix in LAMBDA_FUNCTIONS:
        fn_name = f"{PROJECT}-{env}-{fn_suffix}"
        dims = {"FunctionName": fn_name}

        invocations = cw_sum(cw, "AWS/Lambda", "Invocations", dims, start, end, period)
        errors = cw_sum(cw, "AWS/Lambda", "Errors", dims, start, end, period)
        throttles = cw_sum(cw, "AWS/Lambda", "Throttles", dims, start, end, period)

        duration_p50 = cw_percentile(cw, "AWS/Lambda", "Duration", dims, start, end, period, "p50")
        duration_p99 = cw_percentile(cw, "AWS/Lambda", "Duration", dims, start, end, period, "p99")
        duration_stats = cw_stats(cw, "AWS/Lambda", "Duration", dims, start, end, period, ["Maximum"])

        results[fn_suffix] = {
            "invocations": int(invocations),
            "errors": int(errors),
            "error_rate": round(errors / invocations * 100, 2) if invocations > 0 else 0,
            "throttles": int(throttles),
            "duration_ms": {
                "p50": round(duration_p50, 1),
                "p99": round(duration_p99, 1),
                "max": round(duration_stats.get("Maximum", 0), 1),
            },
        }
    return results


def collect_dynamodb(dynamodb_client, env):
    """Collect DynamoDB table statistics."""
    results = {}
    for table_suffix in DYNAMO_TABLES:
        table_name = f"{PROJECT}-{env}-{table_suffix}"
        try:
            resp = dynamodb_client.describe_table(TableName=table_name)
            table = resp["Table"]
            results[table_suffix] = {
                "item_count": table.get("ItemCount", 0),
                "size_bytes": table.get("TableSizeBytes", 0),
                "size_mb": round(table.get("TableSizeBytes", 0) / (1024 * 1024), 2),
                "status": table.get("TableStatus", "UNKNOWN"),
            }
        except Exception as e:
            results[table_suffix] = {"error": str(e)}
    return results


def collect_ses(cw, start, end, period):
    """Collect SES email metrics."""
    metrics = {}
    for metric_name in ["Send", "Delivery", "Bounce", "Complaint", "Reject"]:
        metrics[metric_name.lower()] = int(cw_sum(cw, "AWS/SES", metric_name, {}, start, end, period))

    sends = metrics.get("send", 0)
    metrics["bounce_rate"] = round(metrics.get("bounce", 0) / sends * 100, 2) if sends > 0 else 0
    metrics["complaint_rate"] = round(metrics.get("complaint", 0) / sends * 100, 2) if sends > 0 else 0
    return metrics


def compute_delta(current, previous):
    """Compute deltas between current and previous snapshot."""
    if not previous:
        return None

    delta = {}

    # DynamoDB item count deltas
    if "dynamodb" in current and "dynamodb" in previous:
        dynamo_delta = {}
        for table in current["dynamodb"]:
            curr = current["dynamodb"].get(table, {}).get("item_count", 0)
            prev = previous.get("dynamodb", {}).get(table, {}).get("item_count", 0)
            diff = curr - prev
            if diff != 0:
                dynamo_delta[table] = {
                    "previous": prev,
                    "current": curr,
                    "change": diff,
                    "change_pct": round(diff / prev * 100, 1) if prev > 0 else None,
                }
        if dynamo_delta:
            delta["dynamodb_item_counts"] = dynamo_delta

    # API request volume delta
    if "api_gateway" in current and "api_gateway" in previous:
        curr_req = current["api_gateway"].get("total_requests", 0)
        prev_req = previous.get("api_gateway", {}).get("total_requests", 0)
        if prev_req > 0:
            delta["api_requests"] = {
                "previous": prev_req,
                "current": curr_req,
                "change": curr_req - prev_req,
                "change_pct": round((curr_req - prev_req) / prev_req * 100, 1),
            }

    # User growth
    if "dynamodb" in current and "dynamodb" in previous:
        curr_users = current["dynamodb"].get("users", {}).get("item_count", 0)
        prev_users = previous.get("dynamodb", {}).get("users", {}).get("item_count", 0)
        delta["user_growth"] = {
            "previous": prev_users,
            "current": curr_users,
            "new_users": curr_users - prev_users,
        }

    return delta


def load_previous_snapshot(env):
    """Load the most recent previous snapshot for comparison."""
    if not SNAPSHOT_DIR.exists():
        return None
    files = sorted(SNAPSHOT_DIR.glob(f"{env}_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Capture system health snapshot.")
    parser.add_argument("--env", default="production", choices=["staging", "production"])
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default: 24)")
    args = parser.parse_args()

    env = args.env
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=args.hours)
    period = args.hours * 3600  # Single data point for the entire window

    print(f"Capturing system snapshot for {env}...")
    print(f"  Window: {start.strftime('%Y-%m-%d %H:%M')} to {now.strftime('%Y-%m-%d %H:%M')} UTC ({args.hours}h)")
    print()

    cw = boto3.client("cloudwatch", region_name=REGION)
    dynamodb_client = boto3.client("dynamodb", region_name=REGION)

    # Collect all metrics
    print("  Collecting API Gateway metrics...")
    api_data = collect_api_gateway(cw, env, start, now, period)

    print("  Collecting Lambda metrics...")
    lambda_data = collect_lambda(cw, env, start, now, period)

    print("  Collecting DynamoDB stats...")
    dynamo_data = collect_dynamodb(dynamodb_client, env)

    print("  Collecting SES metrics...")
    ses_data = collect_ses(cw, start, now, period)

    # Load previous snapshot for delta
    previous = load_previous_snapshot(env)
    previous_file = None
    if previous:
        previous_file = previous.get("metadata", {}).get("captured_at", "unknown")
        print(f"  Previous snapshot: {previous_file}")

    # Build snapshot
    snapshot = {
        "metadata": {
            "environment": env,
            "captured_at": now.isoformat(),
            "window_hours": args.hours,
            "window_start": start.isoformat(),
            "window_end": now.isoformat(),
            "previous_snapshot": previous_file,
        },
        "api_gateway": api_data,
        "lambda": lambda_data,
        "dynamodb": dynamo_data,
        "ses": ses_data,
    }

    # Compute delta if we have a previous snapshot
    delta = compute_delta(snapshot, previous)
    if delta:
        snapshot["delta_from_previous"] = delta

    # Save snapshot
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = f"{env}_{timestamp}.json"
    filepath = SNAPSHOT_DIR / filename

    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2, cls=SnapshotEncoder)

    print()
    print(f"Snapshot saved: {filepath}")
    print()

    # Print summary
    print("=" * 60)
    print(f"  SYSTEM SNAPSHOT — {env.upper()}")
    print(f"  {start.strftime('%b %d %H:%M')} – {now.strftime('%b %d %H:%M')} UTC")
    print("=" * 60)

    print(f"\n  API Gateway:")
    print(f"    Requests:  {api_data['total_requests']:,}")
    print(f"    4xx:       {api_data['4xx_errors']:,}")
    print(f"    5xx:       {api_data['5xx_errors']:,}")
    print(f"    Latency:   p50={api_data['latency_ms']['p50']}ms  p90={api_data['latency_ms']['p90']}ms  p99={api_data['latency_ms']['p99']}ms")

    print(f"\n  Lambda:")
    for fn, data in lambda_data.items():
        err_str = f"  errors={data['errors']}" if data['errors'] > 0 else ""
        print(f"    {fn:20s}  invocations={data['invocations']:>6,}  p50={data['duration_ms']['p50']:>7.0f}ms{err_str}")

    print(f"\n  DynamoDB (item counts):")
    for table, data in dynamo_data.items():
        if "error" not in data:
            print(f"    {table:30s}  {data['item_count']:>10,} items  ({data['size_mb']:.1f} MB)")

    print(f"\n  SES:")
    print(f"    Sent: {ses_data['send']}  Delivered: {ses_data['delivery']}  Bounced: {ses_data['bounce']}  Complaints: {ses_data['complaint']}")

    if delta:
        print(f"\n  Delta (from previous snapshot):")
        if "user_growth" in delta:
            ug = delta["user_growth"]
            print(f"    Users: {ug['previous']} → {ug['current']} (+{ug['new_users']})")
        if "api_requests" in delta:
            ar = delta["api_requests"]
            print(f"    API requests: {ar['previous']:,} → {ar['current']:,} ({ar['change_pct']:+.1f}%)")
        if "dynamodb_item_counts" in delta:
            for table, d in delta["dynamodb_item_counts"].items():
                pct = f" ({d['change_pct']:+.1f}%)" if d.get("change_pct") is not None else ""
                print(f"    {table}: {d['previous']:,} → {d['current']:,} ({d['change']:+,}){pct}")

    print()


if __name__ == "__main__":
    main()
