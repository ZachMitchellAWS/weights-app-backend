#!/usr/bin/env python3
"""Populate the review user with 12 weeks of training data.

The review user (review@liftthebull.io) must already exist in production —
created organically via the app. This script only writes lift sets, estimated
1RMs, and updates hasMetStrengthTierConditions.

Usage:
    python scripts/generate_review_user.py                    # generate data
    python scripts/generate_review_user.py delete              # delete lift sets + e1RMs only
    python scripts/generate_review_user.py --env staging       # target staging
"""

import argparse
import math
import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

# ─── Configuration ─────────────────────────────────────────────────────────────

USER_ID = "b7fc9c9f-3b5e-41bb-b4c4-18d1a0eafeb5"

REGION = "us-west-1"
ENV = "production"
PROJECT = "liftthebull"

NS = uuid.UUID("e1e2e3e4-b5c6-4d7e-8f90-ae01e000cafe")
TIMEZONE = "America/Los_Angeles"

TRAINING_WEEKS = 12
TRAINING_END = datetime.now().replace(hour=18, minute=30, second=0, microsecond=0)
TRAINING_START = TRAINING_END - timedelta(weeks=TRAINING_WEEKS)

STANDARD_EFFORTS = ["easy", "easy", "moderate", "moderate", "hard", "pr"]
DELOAD_EFFORTS = ["easy", "easy", "moderate"]

# ─── Exercise Definitions ──────────────────────────────────────────────────────
# (name, exerciseItemId, loadType, base_e1rm, target_e1rm)

EXERCISES = [
    ("Deadlifts",               "00000000-0000-0000-0001-000000000001", "Barbell",                  315, 450),
    ("Squats",                  "00000000-0000-0000-0001-000000000002", "Barbell",                  255, 365),
    ("Bench Press",             "00000000-0000-0000-0001-000000000003", "Barbell",                  205, 295),
    ("Overhead Press",          "00000000-0000-0000-0001-000000000004", "Barbell",                  125, 165),
    ("Barbell Rows",            "00000000-0000-0000-0001-000000000005", "Barbell",                  175, 235),
    ("Front Squats",            "00000000-0000-0000-0001-000000000044", "Barbell",                  185, 295),
    ("Back Extensions",         "00000000-0000-0000-0001-000000000045", "Single Load",               25,  50),
    ("Hanging Leg Raises",      "00000000-0000-0000-0001-000000000046", "Bodyweight + Single Load",   0,  25),
    ("Bulgarian Split Squats",  "00000000-0000-0000-0001-000000000035", "Single Load",               50,  80),
    ("Romanian Deadlifts",      "00000000-0000-0000-0001-000000000009", "Barbell",                  205, 315),
    ("Standing Calf Raises",    "00000000-0000-0000-0001-000000000039", "Single Load",               70, 120),
    ("Weighted Dips",           "00000000-0000-0000-0001-000000000007", "Bodyweight + Single Load",  45,  90),
    ("Dumbbell Flys",           "00000000-0000-0000-0001-000000000033", "Single Load",               30,  50),
    ("Lateral Raises",          "00000000-0000-0000-0001-000000000032", "Single Load",               20,  35),
    ("Pull Ups",                "00000000-0000-0000-0001-000000000006", "Bodyweight + Single Load",   0,  45),
    ("Barbell Curls",           "00000000-0000-0000-0001-000000000008", "Barbell",                   75, 110),
    ("Barbell Pullovers",       "00000000-0000-0000-0001-000000000070", "Barbell",                   55,  85),
    ("Close Grip Bench Press",  "00000000-0000-0000-0001-000000000041", "Barbell",                  155, 225),
    ("Rear Delt Flys",          "00000000-0000-0000-0001-000000000042", "Single Load",               20,  35),
    ("Cable Y Raises",          "00000000-0000-0000-0001-000000000043", "Single Load",               15,  30),
]

DAY_GROUPS = {
    0: ["Deadlifts", "Front Squats", "Back Extensions", "Hanging Leg Raises"],
    1: ["Bench Press", "Weighted Dips", "Dumbbell Flys", "Lateral Raises"],
    2: ["Squats", "Bulgarian Split Squats", "Romanian Deadlifts", "Standing Calf Raises"],
    3: ["Barbell Rows", "Pull Ups", "Barbell Curls", "Barbell Pullovers"],
    4: ["Overhead Press", "Close Grip Bench Press", "Rear Delt Flys", "Cable Y Raises"],
}

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_table_name(suffix: str) -> str:
    return f"{PROJECT}-{ENV}-{suffix}"


def ts_z(dt: datetime) -> str:
    """ISO8601 with Z suffix — no microseconds, matches client encoder."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def det_uuid(namespace: str) -> str:
    return str(uuid.uuid5(NS, namespace))


def round_weight(weight: float, load_type: str) -> float:
    if load_type == "Barbell":
        return round(weight / 2.5) * 2.5
    else:
        return round(weight / 5.0) * 5.0


def sigmoid_progress(day_index: int, total_days: int) -> float:
    x = (day_index / max(total_days, 1)) * 12 - 6
    return 1.0 / (1.0 + math.exp(-x))


def epley_e1rm(weight: float, reps: int) -> float:
    if reps <= 1:
        return weight
    return weight * (1 + reps / 30.0)


# ─── Data Generation ───────────────────────────────────────────────────────────

def build_exercise_map():
    """Build exercise map from EXERCISES constant."""
    exercise_map = {}
    for name, eid, load_type, base, target in EXERCISES:
        exercise_map[name] = {
            "exerciseItemId": eid,
            "loadType": load_type,
            "base_e1rm": base,
            "target_e1rm": target,
        }
    return exercise_map


def generate_training_sessions():
    sessions = []
    current = TRAINING_START
    week_num = 0
    while current < TRAINING_END:
        week_start = current
        week_num += 1
        is_deload = (week_num % 4 == 0)
        day_count = 3 if is_deload else 5
        for day_offset in range(day_count):
            session_date = week_start + timedelta(days=day_offset)
            if session_date > TRAINING_END:
                break
            hour = 6 + random.randint(0, 1)
            minute = random.randint(0, 30)
            session_dt = session_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            sessions.append({
                "datetime": session_dt,
                "day_of_week": day_offset,
                "is_deload": is_deload,
            })
        current = week_start + timedelta(days=7)
    return sessions


def generate_lift_sets_and_e1rms(sessions, exercise_map):
    rng = random.Random(42)
    total_days = (TRAINING_END - TRAINING_START).days

    lift_sets = []
    e1rms = []
    running_max_e1rm = {}

    for session in sessions:
        session_dt = session["datetime"]
        day_of_week = session["day_of_week"]
        is_deload = session["is_deload"]
        day_index = (session_dt - TRAINING_START).days

        exercise_names = DAY_GROUPS.get(day_of_week, [])
        efforts = DELOAD_EFFORTS if is_deload else STANDARD_EFFORTS

        progress = sigmoid_progress(day_index, total_days)
        daily_noise = 1.0 + rng.uniform(-0.02, 0.02)

        for ex_idx, ex_name in enumerate(exercise_names):
            ex_info = exercise_map[ex_name]
            eid = ex_info["exerciseItemId"]
            load_type = ex_info["loadType"]
            base = ex_info["base_e1rm"]
            target = ex_info["target_e1rm"]
            current_e1rm = base + (target - base) * progress * daily_noise

            for set_idx, effort in enumerate(efforts):
                if effort == "easy":
                    pct = rng.uniform(0.60, 0.70)
                    reps = rng.randint(8, 10)
                elif effort == "moderate":
                    pct = rng.uniform(0.72, 0.82)
                    reps = rng.randint(5, 7)
                elif effort == "hard":
                    pct = rng.uniform(0.84, 0.90)
                    reps = rng.randint(3, 5)
                elif effort == "pr":
                    pct = rng.uniform(0.90, 0.97)
                    reps = rng.randint(1, 3)
                else:
                    pct = rng.uniform(0.55, 0.65)
                    reps = rng.randint(8, 12)

                raw_weight = current_e1rm * pct
                weight = round_weight(max(raw_weight, 0), load_type)

                set_dt = session_dt + timedelta(minutes=ex_idx * 20 + set_idx * 3)
                set_id = det_uuid(f"set-{eid}-{ts_z(set_dt)}-{set_idx}")

                lift_sets.append({
                    "userId": USER_ID,
                    "liftSetId": set_id,
                    "exerciseId": eid,
                    "weight": Decimal(str(weight)),
                    "reps": reps,
                    "createdTimezone": TIMEZONE,
                    "createdDatetime": ts_z(set_dt),
                    "lastModifiedDatetime": ts_z(set_dt),
                })

                computed_e1rm = epley_e1rm(weight, reps)
                prev_max = running_max_e1rm.get(eid, 0)
                running_max_e1rm[eid] = max(prev_max, computed_e1rm)

                e1rm_id = det_uuid(f"e1rm-{eid}-{ts_z(set_dt)}-{set_idx}")
                e1rms.append({
                    "userId": USER_ID,
                    "liftSetId": set_id,
                    "estimated1RMId": e1rm_id,
                    "exerciseId": eid,
                    "value": Decimal(str(round(running_max_e1rm[eid], 2))),
                    "createdTimezone": TIMEZONE,
                    "createdDatetime": ts_z(set_dt),
                    "lastModifiedDatetime": ts_z(set_dt),
                })

    return lift_sets, e1rms


# ─── DynamoDB Operations ──────────────────────────────────────────────────────

def write_items(table_suffix: str, items: list, label: str):
    if not items:
        return
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table_name = get_table_name(table_suffix)
    table = dynamodb.Table(table_name)
    print(f"  Writing {len(items)} {label} to {table_name}...")
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def update_user_properties():
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(get_table_name("user-properties"))
    table.update_item(
        Key={"userId": USER_ID},
        UpdateExpression="SET hasMetStrengthTierConditions = :val",
        ExpressionAttributeValues={":val": True},
    )
    print("  Updated hasMetStrengthTierConditions = true")


def delete_training_data():
    """Delete only lift sets and estimated 1RMs for the review user."""
    dynamodb_resource = boto3.resource("dynamodb", region_name=REGION)
    tables = [
        ("lift-sets", "userId", "liftSetId"),
        ("estimated-1rm", "userId", "liftSetId"),
    ]
    total_deleted = 0
    for suffix, pk, sk in tables:
        table_name = get_table_name(suffix)
        table = dynamodb_resource.Table(table_name)
        kwargs = {"KeyConditionExpression": Key(pk).eq(USER_ID)}
        items = []
        while True:
            response = table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        if not items:
            continue
        with table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={pk: item[pk], sk: item[sk]})
        print(f"  Deleted {len(items)} items from {table_name}")
        total_deleted += len(items)
    print(f"  Total deleted: {total_deleted} items")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Populate review user with training data.")
    parser.add_argument("action", nargs="?", default="generate", choices=["generate", "delete"])
    parser.add_argument("--env", default="production", choices=["staging", "production"])
    args = parser.parse_args()

    global ENV
    ENV = args.env

    if args.action == "delete":
        print(f"Deleting training data for review user ({USER_ID}) from {ENV}...")
        delete_training_data()
        print("Done!")
        return

    random.seed(42)

    print(f"Generating review user training data (12 weeks)...")
    print(f"  User: {USER_ID}")
    print(f"  Environment: {ENV}")
    print(f"  Training window: {TRAINING_START.date()} to {TRAINING_END.date()}")
    print()

    exercise_map = build_exercise_map()
    sessions = generate_training_sessions()
    print(f"  {len(sessions)} training sessions")

    lift_sets, e1rms = generate_lift_sets_and_e1rms(sessions, exercise_map)
    print(f"  {len(lift_sets)} lift sets")
    print(f"  {len(e1rms)} estimated 1RMs")
    print()

    write_items("lift-sets", lift_sets, "lift sets")
    write_items("estimated-1rm", e1rms, "estimated 1RMs")
    update_user_properties()

    print()
    print("Done!")


if __name__ == "__main__":
    main()
