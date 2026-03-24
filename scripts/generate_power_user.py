#!/usr/bin/env python3
"""Generate and load a realistic "power user" dataset into staging DynamoDB.

Simulates a heavy user with N months of training history (~65 exercises, 28 lift sets
per training day, ~50 sequences). Each training day produces exactly 28 sets (16 on
deload weeks). Data is deterministic via uuid5 and seeded random, making re-runs
idempotent (put_item is an upsert).

Usage:
    python scripts/generate_power_user.py                # default 12 months
    python scripts/generate_power_user.py --months 6     # 6 months of history
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

POWER_USER_ID = "a0b1c2d3-e4f5-4000-8000-power00000001"
EMAIL = "zmitc002+power@gmail.com"
# SHA256("password" + staging pepper) — login with "password"
PASSWORD_HASH = "6b5d9ba5e3a7c62a4c3750d66591a003053d48695949bf3f117c0354f1cdc9cc"

REGION = "us-west-1"
ENV = "staging"
PROJECT = "liftthebull"

# Deterministic UUID namespace
NS = uuid.UUID("d1e2f3a4-b5c6-4d7e-8f90-a1b2c3d4e5f6")

# Training window — ends today, start computed from --months
TRAINING_END = datetime.now().replace(hour=6, minute=0, second=0, microsecond=0)

# Fixed account creation date (does not shift with training window)
USER_CREATED = datetime(2024, 2, 20, 6, 0, 0)

TIMEZONE = "America/Los_Angeles"

# ─── Exercise Catalog ──────────────────────────────────────────────────────────

# Default setPlan array matching what the iOS app generates
DEFAULT_SET_PLAN = ["easy", "easy", "moderate", "moderate", "hard", "hard", "pr"]

# Default exercise names that match what the iOS app creates (isCustom=False).
# These get specific icons via the suggestedIcon() logic; all others get LiftTheBullIcon.
DEFAULT_EXERCISE_NAMES = {
    "Deadlift", "Squat", "Bench Press", "Overhead Press",
    "Barbell Row", "Pull Ups", "Dips", "Barbell Curls",
}

# Icon mapping mirroring IconCarouselPicker.suggestedIcon(for:) on iOS.
# Keywords are checked in order; first match wins. Fallback is LiftTheBullIcon.
def suggested_icon(name: str) -> str:
    lowered = name.lower()
    if "overhead" in lowered and "press" in lowered:
        return "OverheadPressIcon"
    if "bench" in lowered:
        return "BenchPressIcon"
    if "row" in lowered:
        return "BarbellRowIcon"
    if "pull" in lowered and "up" in lowered:
        return "PullUpIcon"
    if "deadlift" in lowered:
        return "DeadliftIcon"
    if "squat" in lowered:
        return "SquatIcon"
    if "dip" in lowered:
        return "DipsIcon"
    if "curl" in lowered:
        return "CurlsIcon"
    return "LiftTheBullIcon"

# (name, loadType, movementType, base_1rm, peak_1rm)
EXERCISE_CATALOG = [
    # Barbell — Push
    ("Bench Press", "Barbell", "Push", 185, 265),
    ("Incline Bench Press", "Barbell", "Push", 155, 225),
    ("Overhead Press", "Barbell", "Push", 115, 165),
    ("Close-Grip Bench Press", "Barbell", "Push", 155, 225),
    ("Push Press", "Barbell", "Push", 135, 195),
    ("Floor Press", "Barbell", "Push", 165, 240),
    # Barbell — Pull
    ("Barbell Row", "Barbell", "Pull", 155, 235),
    ("Pendlay Row", "Barbell", "Pull", 145, 215),
    ("T-Bar Row", "Barbell", "Pull", 135, 205),
    ("Barbell Curls", "Barbell", "Pull", 75, 110),
    ("Barbell Shrug", "Barbell", "Pull", 185, 315),
    # Barbell — Squat
    ("Squat", "Barbell", "Squat", 225, 365),
    ("Front Squat", "Barbell", "Squat", 185, 295),
    ("Pause Squat", "Barbell", "Squat", 195, 305),
    ("Box Squat", "Barbell", "Squat", 205, 315),
    ("Zercher Squat", "Barbell", "Squat", 155, 245),
    # Barbell — Hinge
    ("Deadlift", "Barbell", "Hinge", 275, 425),
    ("Romanian Deadlift", "Barbell", "Hinge", 205, 315),
    ("Sumo Deadlift", "Barbell", "Hinge", 265, 405),
    ("Stiff-Leg Deadlift", "Barbell", "Hinge", 185, 285),
    ("Hip Thrust", "Barbell", "Hinge", 225, 365),
    ("Good Morning", "Barbell", "Hinge", 135, 205),
    # Barbell — Core
    ("Barbell Rollout", "Barbell", "Core", 45, 75),
    ("Landmine Rotation", "Barbell", "Core", 55, 85),
    # Barbell — Other
    ("Power Clean", "Barbell", "Other", 155, 225),
    ("Hang Clean", "Barbell", "Other", 145, 215),
    ("Clean and Jerk", "Barbell", "Other", 155, 225),
    ("Snatch", "Barbell", "Other", 115, 175),
    ("Barbell Lunge", "Barbell", "Other", 115, 185),
    ("Barbell Step-Up", "Barbell", "Other", 95, 155),
    ("Viking Press", "Barbell", "Other", 135, 205),
    ("Barbell Calf Raise", "Barbell", "Other", 185, 275),
    # Single Load — Push
    ("Dips", "Single Load", "Push", 45, 90),
    ("Dumbbell Bench Press", "Single Load", "Push", 70, 100),
    ("Dumbbell Shoulder Press", "Single Load", "Push", 50, 75),
    ("Dumbbell Incline Press", "Single Load", "Push", 60, 90),
    ("Cable Fly", "Single Load", "Push", 30, 55),
    ("Lateral Raise", "Single Load", "Push", 20, 35),
    ("Tricep Pushdown", "Single Load", "Push", 50, 80),
    ("Overhead Tricep Extension", "Single Load", "Push", 40, 65),
    ("Machine Chest Press", "Single Load", "Push", 140, 220),
    # Single Load — Pull
    ("Pull Ups", "Single Load", "Pull", 0, 45),
    ("Dumbbell Row", "Single Load", "Pull", 70, 110),
    ("Cable Row", "Single Load", "Pull", 120, 190),
    ("Lat Pulldown", "Single Load", "Pull", 130, 200),
    ("Face Pull", "Single Load", "Pull", 40, 65),
    ("Dumbbell Curl", "Single Load", "Pull", 30, 50),
    ("Hammer Curl", "Single Load", "Pull", 35, 55),
    ("Cable Curl", "Single Load", "Pull", 40, 65),
    ("Machine Row", "Single Load", "Pull", 130, 200),
    # Single Load — Squat
    ("Goblet Squat", "Single Load", "Squat", 60, 100),
    ("Leg Press", "Single Load", "Squat", 360, 580),
    ("Leg Extension", "Single Load", "Squat", 100, 160),
    ("Bulgarian Split Squat", "Single Load", "Squat", 50, 80),
    # Single Load — Hinge
    ("Dumbbell RDL", "Single Load", "Hinge", 60, 100),
    ("Leg Curl", "Single Load", "Hinge", 80, 130),
    ("Cable Pull-Through", "Single Load", "Hinge", 60, 100),
    ("Kettlebell Swing", "Single Load", "Hinge", 53, 88),
    # Single Load — Core
    ("Cable Crunch", "Single Load", "Core", 80, 130),
    ("Cable Woodchop", "Single Load", "Core", 40, 65),
    ("Dumbbell Side Bend", "Single Load", "Core", 50, 80),
    # Single Load — Other
    ("Dumbbell Lunge", "Single Load", "Other", 50, 80),
    ("Machine Calf Raise", "Single Load", "Other", 160, 260),
    ("Dumbbell Shrug", "Single Load", "Other", 70, 110),
    ("Reverse Fly", "Single Load", "Other", 20, 35),
    ("Machine Lateral Raise", "Single Load", "Other", 60, 100),
    ("Wrist Curl", "Single Load", "Other", 30, 50),
]


# ─── Sequence Templates ───────────────────────────────────────────────────────

# (name, list of exercise name references)
SEQUENCE_TEMPLATES = [
    # Full Body (Phase 1)
    ("Full Body A", ["Squat", "Bench Press", "Barbell Row", "Overhead Press", "Barbell Curls", "Cable Crunch"]),
    ("Full Body B", ["Deadlift", "Incline Bench Press", "Lat Pulldown", "Dumbbell Shoulder Press", "Hammer Curl", "Cable Woodchop"]),
    ("Full Body C", ["Front Squat", "Close-Grip Bench Press", "Dumbbell Row", "Lateral Raise", "Leg Curl", "Dumbbell Side Bend"]),
    # Push/Pull/Legs (Phase 2-3)
    ("Push A", ["Bench Press", "Overhead Press", "Dips", "Dumbbell Incline Press", "Cable Fly", "Tricep Pushdown", "Lateral Raise"]),
    ("Push B", ["Incline Bench Press", "Push Press", "Machine Chest Press", "Overhead Tricep Extension", "Lateral Raise", "Cable Fly"]),
    ("Pull A", ["Barbell Row", "Pull Ups", "Face Pull", "Barbell Curls", "Hammer Curl", "Barbell Shrug"]),
    ("Pull B", ["Pendlay Row", "Cable Row", "Dumbbell Row", "Cable Curl", "Dumbbell Curl", "Face Pull"]),
    ("Legs Squat", ["Squat", "Front Squat", "Leg Press", "Leg Extension", "Bulgarian Split Squat", "Machine Calf Raise"]),
    ("Legs Hinge", ["Deadlift", "Romanian Deadlift", "Leg Curl", "Hip Thrust", "Cable Pull-Through", "Machine Calf Raise"]),
    # Specialization (Phase 3)
    ("Bench Specialization", ["Bench Press", "Close-Grip Bench Press", "Floor Press", "Dumbbell Bench Press", "Tricep Pushdown"]),
    ("Squat Specialization", ["Squat", "Pause Squat", "Box Squat", "Leg Press", "Leg Extension"]),
    ("Deadlift Specialization", ["Deadlift", "Sumo Deadlift", "Stiff-Leg Deadlift", "Barbell Row", "Leg Curl"]),
    ("OHP Specialization", ["Overhead Press", "Push Press", "Viking Press", "Dumbbell Shoulder Press", "Lateral Raise"]),
    ("Back Specialization", ["Barbell Row", "T-Bar Row", "Machine Row", "Lat Pulldown", "Face Pull"]),
    ("Arm Day", ["Barbell Curls", "Hammer Curl", "Cable Curl", "Tricep Pushdown", "Overhead Tricep Extension"]),
    ("Olympic Lifting", ["Power Clean", "Hang Clean", "Clean and Jerk", "Snatch", "Front Squat"]),
    # Extra variety
    ("Upper Body A", ["Bench Press", "Barbell Row", "Overhead Press", "Pull Ups", "Barbell Curls", "Dips"]),
    ("Upper Body B", ["Incline Bench Press", "Pendlay Row", "Dumbbell Shoulder Press", "Cable Row", "Hammer Curl", "Cable Fly"]),
    ("Lower Body A", ["Squat", "Romanian Deadlift", "Leg Press", "Leg Curl", "Machine Calf Raise"]),
    ("Lower Body B", ["Deadlift", "Front Squat", "Bulgarian Split Squat", "Leg Extension", "Machine Calf Raise"]),
    ("Push Hypertrophy", ["Machine Chest Press", "Dumbbell Incline Press", "Cable Fly", "Machine Lateral Raise", "Overhead Tricep Extension"]),
    ("Pull Hypertrophy", ["Machine Row", "Lat Pulldown", "Cable Row", "Face Pull", "Dumbbell Curl", "Reverse Fly"]),
    ("Legs Hypertrophy", ["Leg Press", "Goblet Squat", "Leg Extension", "Leg Curl", "Machine Calf Raise", "Cable Pull-Through"]),
    ("Core Focus", ["Cable Crunch", "Cable Woodchop", "Dumbbell Side Bend", "Barbell Rollout", "Landmine Rotation"]),
    ("Posterior Chain", ["Deadlift", "Good Morning", "Hip Thrust", "Leg Curl", "Kettlebell Swing"]),
    ("Full Body D", ["Squat", "Overhead Press", "Barbell Row", "Dumbbell RDL", "Barbell Curls"]),
    ("Full Body E", ["Deadlift", "Bench Press", "Lat Pulldown", "Goblet Squat", "Cable Crunch"]),
    ("Push C", ["Close-Grip Bench Press", "Dumbbell Shoulder Press", "Machine Chest Press", "Lateral Raise", "Tricep Pushdown"]),
    ("Pull C", ["T-Bar Row", "Lat Pulldown", "Dumbbell Row", "Face Pull", "Barbell Curls", "Dumbbell Shrug"]),
    ("Legs C", ["Zercher Squat", "Stiff-Leg Deadlift", "Leg Press", "Leg Curl", "Dumbbell Lunge", "Machine Calf Raise"]),
    ("Strength Test Day", ["Squat", "Bench Press", "Deadlift"]),
    ("Accessory Day", ["Lateral Raise", "Face Pull", "Hammer Curl", "Tricep Pushdown", "Cable Crunch", "Machine Calf Raise"]),
    ("Power Day", ["Power Clean", "Push Press", "Box Squat", "Barbell Row"]),
    ("GPP Day", ["Kettlebell Swing", "Goblet Squat", "Dumbbell Lunge", "Cable Woodchop", "Machine Calf Raise"]),
    ("Pressing Focus", ["Bench Press", "Overhead Press", "Floor Press", "Dumbbell Bench Press", "Dumbbell Shoulder Press"]),
    ("Rowing Focus", ["Barbell Row", "Pendlay Row", "Cable Row", "Machine Row", "Dumbbell Row"]),
    ("Leg Day Heavy", ["Squat", "Deadlift", "Leg Press", "Hip Thrust"]),
    ("Recovery Day", ["Cable Crunch", "Face Pull", "Lateral Raise", "Wrist Curl", "Machine Calf Raise"]),
    ("Volume Bench", ["Bench Press", "Incline Bench Press", "Close-Grip Bench Press", "Dumbbell Bench Press"]),
    ("Volume Squat", ["Squat", "Front Squat", "Pause Squat", "Goblet Squat"]),
    ("Push Pull A", ["Bench Press", "Barbell Row", "Overhead Press", "Lat Pulldown", "Tricep Pushdown", "Barbell Curls"]),
    ("Push Pull B", ["Incline Bench Press", "Cable Row", "Dumbbell Shoulder Press", "Face Pull", "Cable Fly", "Hammer Curl"]),
    ("Athletic Day", ["Power Clean", "Box Squat", "Push Press", "Barbell Lunge", "Barbell Step-Up", "Kettlebell Swing"]),
    ("Isolation Focus", ["Lateral Raise", "Reverse Fly", "Leg Extension", "Leg Curl", "Dumbbell Curl", "Tricep Pushdown"]),
    ("Compound Only", ["Squat", "Bench Press", "Deadlift", "Overhead Press", "Barbell Row"]),
    ("Dumbbell Day", ["Dumbbell Bench Press", "Dumbbell Row", "Dumbbell Shoulder Press", "Dumbbell RDL", "Dumbbell Curl", "Dumbbell Lunge"]),
    ("Machine Day", ["Machine Chest Press", "Machine Row", "Leg Press", "Leg Extension", "Leg Curl", "Machine Calf Raise"]),
    ("Cable Day", ["Cable Fly", "Cable Row", "Cable Curl", "Tricep Pushdown", "Cable Crunch", "Cable Pull-Through"]),
    ("Barbell Complex", ["Power Clean", "Front Squat", "Overhead Press", "Barbell Row", "Romanian Deadlift"]),
    ("Weak Point Day", ["Pause Squat", "Floor Press", "Pendlay Row", "Good Morning", "Cable Crunch", "Face Pull"]),
]


# ─── Helpers ───────────────────────────────────────────────────────────────────

def det_uuid(label: str) -> str:
    """Generate a deterministic UUID from a label string (uppercase to match iOS)."""
    return str(uuid.uuid5(NS, label)).upper()


def ts_no_z(dt: datetime) -> str:
    """Format datetime for users/user-properties (no Z suffix)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def ts_z(dt: datetime) -> str:
    """Format datetime for exercises/lift-sets/etc (Z suffix)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def round_weight(w: float, load_type: str) -> float:
    """Round weight to nearest plate increment."""
    if load_type == "Barbell":
        return round(w / 2.5) * 2.5
    else:
        return round(w / 5) * 5


def sigmoid_progress(t: float) -> float:
    """Sigmoid curve from 0 to 1, with t in [0, 1]. Slow start, fast middle, plateau."""
    return 1 / (1 + math.exp(-10 * (t - 0.5)))


def convert_floats_to_decimal(obj):
    """Recursively convert all floats to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_floats_to_decimal(item) for item in obj]
    return obj


def get_table_name(suffix: str) -> str:
    return f"{PROJECT}-{ENV}-{suffix}"


# ─── Data Generation ──────────────────────────────────────────────────────────

def generate_exercises(training_start):
    """Generate exercise items from the catalog."""
    exercises = []
    exercise_map = {}  # name -> exerciseItemId

    created = training_start - timedelta(days=1)

    for i, (name, load_type, movement_type, base_1rm, peak_1rm) in enumerate(EXERCISE_CATALOG):
        eid = det_uuid(f"exercise-{i}-{name}")
        exercise_map[name] = {
            "exerciseItemId": eid,
            "loadType": load_type,
            "base_1rm": base_1rm,
            "peak_1rm": peak_1rm,
        }

        is_custom = name not in DEFAULT_EXERCISE_NAMES
        icon = suggested_icon(name)

        exercises.append({
            "userId": POWER_USER_ID,
            "exerciseItemId": eid,
            "name": name,
            "isCustom": is_custom,
            "loadType": load_type,
            "icon": icon,
            "createdTimezone": TIMEZONE,
            "createdDatetime": ts_z(created + timedelta(minutes=i)),
            "lastModifiedDatetime": ts_z(created + timedelta(minutes=i)),
            "movementType": movement_type,
            "setPlan": DEFAULT_SET_PLAN,
        })

    return exercises, exercise_map


def generate_training_calendar(training_start):
    """Generate training session dates across 3 phases.

    Phase boundaries scale proportionally to the total training window:
    Phase 1 (~first 15%): 3 days/week full-body
    Phase 2 (~next 35%): 5 days/week PPL
    Phase 3 (remainder): 5-6 days/week PPL + specialization
    """
    sessions = []
    current = training_start

    total_weeks = max(1, (TRAINING_END - training_start).days // 7)
    phase1_end = max(4, total_weeks // 6)
    phase2_end = max(phase1_end + 4, total_weeks // 2)

    week_num = 0
    while current < TRAINING_END:
        week_start = current
        week_num += 1
        is_deload = (week_num % 4 == 0)

        # Determine phase
        if week_num <= phase1_end:
            # Phase 1: 3 days/week (Mon, Wed, Fri)
            days = [0, 2, 4]  # Mon, Wed, Fri
            phase = 1
        elif week_num <= phase2_end:
            # Phase 2: 5 days/week (Mon-Fri)
            days = [0, 1, 2, 3, 4]
            phase = 2
        else:
            # Phase 3: 5-6 days/week
            days = [0, 1, 2, 3, 4, 5] if not is_deload else [0, 1, 2, 3, 4]
            phase = 3

        if is_deload:
            # Deload: reduce days
            days = days[:max(2, len(days) - 1)]

        for day_offset in days:
            session_date = week_start + timedelta(days=day_offset)
            if session_date >= TRAINING_END:
                break
            # Session time: 6:00-7:30 AM with some variance
            hour = 6
            minute = random.randint(0, 30)
            session_dt = session_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            sessions.append({
                "datetime": session_dt,
                "phase": phase,
                "is_deload": is_deload,
                "week_num": week_num,
            })

        current = week_start + timedelta(days=7)

    return sessions


def pick_session_exercises(session, exercise_map, rng):
    """Pick exercises for a session based on phase and type."""
    phase = session["phase"]
    is_deload = session["is_deload"]
    day_of_week = session["datetime"].weekday()

    all_names = list(exercise_map.keys())

    by_movement = {}
    for name, _, mt, _, _ in EXERCISE_CATALOG:
        by_movement.setdefault(mt, []).append(name)

    if phase == 1:
        # Full body: 2-3 from each major group → ~10 exercises
        chosen = []
        for mt in ["Squat", "Push", "Pull", "Hinge"]:
            chosen.extend(rng.sample(by_movement[mt], min(3, len(by_movement[mt]))))
        chosen.extend(rng.sample(by_movement["Core"], 1))
        chosen.extend(rng.sample(by_movement["Other"], 1))
    elif phase == 2 or phase == 3:
        # PPL rotation — 8-12 exercises per session
        if day_of_week in [0, 3]:  # Mon, Thu = Push
            chosen = rng.sample(by_movement["Push"], min(8, len(by_movement["Push"])))
            chosen.extend(rng.sample(by_movement["Core"], min(2, len(by_movement["Core"]))))
        elif day_of_week in [1, 4]:  # Tue, Fri = Pull
            chosen = rng.sample(by_movement["Pull"], min(8, len(by_movement["Pull"])))
            chosen.extend(rng.sample(by_movement["Other"], min(2, len(by_movement["Other"]))))
        elif day_of_week == 2:  # Wed = Legs
            chosen = rng.sample(by_movement["Squat"], min(5, len(by_movement["Squat"])))
            chosen.extend(rng.sample(by_movement["Hinge"], min(5, len(by_movement["Hinge"]))))
        elif day_of_week == 5:  # Sat = specialization (phase 3)
            mt = rng.choice(["Push", "Pull", "Squat", "Hinge"])
            chosen = rng.sample(by_movement[mt], min(6, len(by_movement[mt])))
            chosen.extend(rng.sample(by_movement["Core"], min(2, len(by_movement["Core"]))))
            other_mt = rng.choice([m for m in ["Push", "Pull", "Squat", "Hinge"] if m != mt])
            chosen.extend(rng.sample(by_movement[other_mt], min(2, len(by_movement[other_mt]))))
        else:
            chosen = rng.sample(all_names, 8)
    else:
        chosen = rng.sample(all_names, 8)

    if is_deload:
        chosen = chosen[:max(4, len(chosen) - 3)]

    return chosen[:12]  # Cap at 12 exercises per session


def generate_lift_sets_and_e1rms(sessions, exercise_map, training_start):
    """Generate lift sets and corresponding estimated 1RMs.

    Each training day gets exactly 28 sets (16 on deload weeks), distributed
    evenly across the chosen exercises.
    """
    SETS_PER_DAY = 28
    DELOAD_SETS_PER_DAY = 16

    rng = random.Random(42)

    print(f"  Training sessions: {len(sessions)}")
    print(f"  Sets per day: {SETS_PER_DAY} (deload: {DELOAD_SETS_PER_DAY})")

    total_days = (TRAINING_END - training_start).days
    lift_sets = []
    e1rms = []

    for session_idx, session in enumerate(sessions):
        session_dt = session["datetime"]
        phase = session["phase"]
        is_deload = session["is_deload"]

        chosen_exercises = pick_session_exercises(session, exercise_map, rng)

        # Distribute sets evenly across exercises
        day_sets = DELOAD_SETS_PER_DAY if is_deload else SETS_PER_DAY
        n_ex = len(chosen_exercises)
        base_sets = day_sets // n_ex
        remainder = day_sets % n_ex

        # Progress through training (0 to 1)
        days_in = (session_dt - training_start).days
        progress = days_in / total_days

        is_first_compound = True

        for ex_idx, ex_name in enumerate(chosen_exercises):
            ex_info = exercise_map[ex_name]
            eid = ex_info["exerciseItemId"]
            load_type = ex_info["loadType"]
            base = ex_info["base_1rm"]
            peak = ex_info["peak_1rm"]

            # Current 1RM based on sigmoid progression + noise
            current_1rm = base + (peak - base) * sigmoid_progress(progress)
            current_1rm *= (1 + rng.gauss(0, 0.02))  # 2% noise

            # This exercise's set count (first exercises get the remainder)
            num_sets = base_sets + (1 if ex_idx < remainder else 0)

            # Phase-based rep/intensity scheme
            if is_deload:
                reps = rng.randint(8, 12)
                intensity = rng.uniform(0.55, 0.65)
            elif phase == 1:
                reps = rng.randint(6, 12)
                intensity = rng.uniform(0.65, 0.80)
            elif phase == 2:
                reps = rng.randint(4, 8)
                intensity = rng.uniform(0.72, 0.88)
            else:  # phase 3
                reps = rng.randint(1, 6)
                intensity = rng.uniform(0.78, 0.95)

            for s in range(num_sets):
                # Vary intensity slightly per set (ramp up)
                set_intensity = intensity * (0.92 + 0.08 * (s / max(1, num_sets - 1)))
                weight = round_weight(current_1rm * set_intensity, load_type)
                weight = max(5.0 if load_type == "Single Load" else 45.0, weight)

                # Vary reps slightly per set
                set_reps = max(1, reps + rng.randint(-1, 1))

                # RIR based on intensity
                if set_intensity >= 0.95:
                    rir = 0
                elif set_intensity >= 0.90:
                    rir = rng.randint(0, 1)
                elif set_intensity >= 0.85:
                    rir = rng.randint(1, 2)
                elif set_intensity >= 0.78:
                    rir = rng.randint(2, 3)
                else:
                    rir = rng.randint(3, 4)

                # Timestamps: spread sets across the session (~1 min apart)
                set_dt = session_dt + timedelta(minutes=ex_idx * 8 + s * 2 + rng.randint(0, 1))
                set_id = det_uuid(f"liftset-{session_idx}-{ex_idx}-{s}")

                ls = {
                    "userId": POWER_USER_ID,
                    "liftSetId": set_id,
                    "exerciseId": eid,
                    "reps": set_reps,
                    "weight": weight,
                    "createdTimezone": TIMEZONE,
                    "createdDatetime": ts_z(set_dt),
                    "lastModifiedDatetime": ts_z(set_dt),
                    "rir": rir,
                }

                # isBaselineSet: first heavy compound of the session
                is_compound = ex_info.get("loadType") == "Barbell" and \
                    any(name == ex_name and mt in ("Squat", "Hinge", "Push", "Pull")
                        for name, _, mt, _, _ in EXERCISE_CATALOG)
                if is_first_compound and is_compound and s == 0 and set_intensity >= 0.80:
                    ls["isBaselineSet"] = True
                    is_first_compound = False

                lift_sets.append(ls)

                # Corresponding E1RM (Epley formula)
                if set_reps == 1:
                    e1rm_value = weight
                else:
                    e1rm_value = weight * (1 + set_reps / 30.0)
                e1rm_value = round(e1rm_value, 1)

                e1rm_id = det_uuid(f"e1rm-{session_idx}-{ex_idx}-{s}")
                e1rms.append({
                    "userId": POWER_USER_ID,
                    "liftSetId": set_id,
                    "estimated1RMId": e1rm_id,
                    "exerciseId": eid,
                    "value": e1rm_value,
                    "createdTimezone": TIMEZONE,
                    "createdDatetime": ts_z(set_dt),
                    "lastModifiedDatetime": ts_z(set_dt),
                })

    return lift_sets, e1rms


def generate_sequences(exercise_map, training_start):
    """Generate sequence items from templates."""
    sequences = []
    created = training_start - timedelta(hours=12)

    for i, (name, ex_names) in enumerate(SEQUENCE_TEMPLATES):
        ex_ids = []
        for en in ex_names:
            if en in exercise_map:
                ex_ids.append(exercise_map[en]["exerciseItemId"])
        if not ex_ids:
            continue

        seq_id = det_uuid(f"sequence-{i}-{name}")
        seq_dt = created + timedelta(minutes=i * 5)
        sequences.append({
            "userId": POWER_USER_ID,
            "sequenceId": seq_id,
            "name": name,
            "exerciseIds": ex_ids,
            "createdTimezone": TIMEZONE,
            "createdDatetime": ts_z(seq_dt),
            "lastModifiedDatetime": ts_z(seq_dt),
        })

    return sequences


def generate_static_records():
    """Generate user, user-properties, entitlement-grant, subscription-event."""
    created_str = ts_no_z(USER_CREATED)

    user = {
        "userId": POWER_USER_ID,
        "emailAddress": EMAIL,
        "passwordHash": PASSWORD_HASH,
        "createdDatetime": created_str,
        "lastModifiedDatetime": created_str,
    }

    user_props = {
        "userId": POWER_USER_ID,
        "bodyweight": 195.0,
        "availableChangePlates": [2.5, 5, 10, 25, 35, 45],
        "minReps": 3,
        "maxReps": 5,
        "createdDatetime": created_str,
        "lastModifiedDatetime": created_str,
    }

    entitlement_end = TRAINING_END + timedelta(days=365)
    entitlement = {
        "userId": POWER_USER_ID,
        "startUtc": "2024-02-20T00:00:00Z",
        "endUtc": ts_z(entitlement_end),
        "entitlementName": "premium",
        "paymentPlatformSource": "app_store",
        "originalTransactionId": "2000000799999999",
        "productId": "com.liftthebull.annual",
        "createdDatetime": created_str,
        "lastModifiedDatetime": created_str,
    }

    subscription_event = {
        "userId": POWER_USER_ID,
        "eventTimestamp": "2024-02-20T06:00:00Z",
        "notificationType": "INITIAL_BUY",
        "originalTransactionId": "2000000799999999",
        "transactionId": "2000000799999999",
        "productId": "com.liftthebull.annual",
        "purchaseDateMs": int(USER_CREATED.timestamp() * 1000),
        "expiresDateMs": int(entitlement_end.timestamp() * 1000),
    }

    return user, user_props, entitlement, subscription_event


# ─── DynamoDB Write ────────────────────────────────────────────────────────────

def write_items(table_suffix: str, items: list, label: str):
    """Write items to a DynamoDB table using batch_writer."""
    if not items:
        return

    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table_name = get_table_name(table_suffix)
    table = dynamodb.Table(table_name)

    print(f"  Writing {len(items)} {label} to {table_name}...")

    count = 0
    with table.batch_writer() as batch:
        for item in items:
            converted = convert_floats_to_decimal(item)
            batch.put_item(Item=converted)
            count += 1
            if count % 2000 == 0:
                print(f"    ... {count}/{len(items)}")

    print(f"    Done: {count} items written")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate and load power user data into staging DynamoDB."
    )
    parser.add_argument(
        "--months",
        type=int,
        default=12,
        help="Number of months of training history to generate (default: 12)",
    )
    args = parser.parse_args()

    training_start = TRAINING_END - timedelta(days=args.months * 30)

    print(f"Generating power user data ({args.months} months of history)...")
    print(f"  User: {EMAIL} ({POWER_USER_ID})")
    print(f"  Training window: {training_start.date()} to {TRAINING_END.date()} ({args.months * 30} days)")
    print()

    # Seed for deterministic exercise selection in sessions
    random.seed(42)

    # Generate all data
    print("Generating exercises...")
    exercises, exercise_map = generate_exercises(training_start)
    print(f"  {len(exercises)} exercises")

    print("Generating training calendar...")
    sessions = generate_training_calendar(training_start)
    print(f"  {len(sessions)} training sessions")

    print("Generating lift sets and estimated 1RMs...")
    lift_sets, e1rms = generate_lift_sets_and_e1rms(sessions, exercise_map, training_start)
    print(f"  {len(lift_sets)} lift sets")
    print(f"  {len(e1rms)} estimated 1RMs")

    print("Generating sequences...")
    sequences = generate_sequences(exercise_map, training_start)
    print(f"  {len(sequences)} sequences")

    print("Generating static records...")
    user, user_props, entitlement, sub_event = generate_static_records()

    print()
    print("=" * 60)
    print(f"Summary: {len(exercises)} exercises, {len(sequences)} sequences, "
          f"{len(lift_sets)} lift sets, {len(e1rms)} estimated 1RMs")
    print("=" * 60)
    print()

    # Write to DynamoDB
    print("Writing to DynamoDB staging tables...")
    write_items("users", [user], "user record")
    write_items("user-properties", [user_props], "user properties")
    write_items("exercises", exercises, "exercises")
    write_items("lift-sets", lift_sets, "lift sets")
    write_items("estimated-1rm", e1rms, "estimated 1RMs")
    write_items("sequences", sequences, "sequences")
    write_items("entitlement-grants", [entitlement], "entitlement grant")
    write_items("subscription-events", [sub_event], "subscription event")

    print()
    print("Done! Power user data loaded into staging.")


if __name__ == "__main__":
    main()
