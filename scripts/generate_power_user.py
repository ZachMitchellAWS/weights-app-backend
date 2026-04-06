#!/usr/bin/env python3
"""Generate and load a realistic "power user" dataset into staging DynamoDB.

Simulates a heavy user with N months of training history (~65 exercises, 28 lift sets
per training day). Each training day produces exactly 28 sets (16 on
deload weeks). Data is deterministic via uuid5 and seeded random, making re-runs
idempotent (put_item is an upsert).

Usage:
    python scripts/generate_power_user.py                          # default 12 months ending today
    python scripts/generate_power_user.py --months 6               # 6 months ending today
    python scripts/generate_power_user.py --months 24 --stale 6    # 24 months ending 6 months ago
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

# Authoritative UUID mapping from the iOS app's Exercise.builtInTemplates.
# Exercises matching these names use the app's UUIDs so lift sets/e1RMs
# reference the same IDs the app expects for strength tiers, groups, etc.
BUILTIN_EXERCISE_UUIDS = {
    "Ab Wheel Rollouts": "00000000-0000-0000-0001-000000000126",
    "Alternating Front Raises": "00000000-0000-0000-0001-000000000052",
    "Arnold Presses": "00000000-0000-0000-0001-000000000050",
    "Back Extensions": "00000000-0000-0000-0001-000000000045",
    "Back Presses": "00000000-0000-0000-0001-000000000047",
    "Barbell Curls": "00000000-0000-0000-0001-000000000008",
    "Barbell Front Raises": "00000000-0000-0000-0001-000000000053",
    "Barbell Lunges": "00000000-0000-0000-0001-000000000104",
    "Barbell Pullovers": "00000000-0000-0000-0001-000000000070",
    "Barbell Rows": "00000000-0000-0000-0001-000000000005",
    "Barbell Shrugs": "00000000-0000-0000-0001-000000000036",
    "Bench Press": "00000000-0000-0000-0001-000000000003",
    "Bent Over Dumbbell Rows": "00000000-0000-0000-0001-000000000078",
    "Bent Over Lateral Raises": "00000000-0000-0000-0001-000000000051",
    "Box Squats": "00000000-0000-0000-0001-000000000094",
    "Bulgarian Split Squats": "00000000-0000-0000-0001-000000000035",
    "Cable Crunches": "00000000-0000-0000-0001-000000000114",
    "Cable Flys": "00000000-0000-0000-0001-000000000069",
    "Cable Hip Abductions": "00000000-0000-0000-0001-000000000109",
    "Cable Hip Adductions": "00000000-0000-0000-0001-000000000099",
    "Cable Kickbacks": "00000000-0000-0000-0001-000000000106",
    "Cable Lateral Raises": "00000000-0000-0000-0001-000000000131",
    "Cable Rows": "00000000-0000-0000-0001-000000000040",
    "Cable Woodchops": "00000000-0000-0000-0001-000000000128",
    "Cable Y Raises": "00000000-0000-0000-0001-000000000043",
    "Chin-Ups": "00000000-0000-0000-0001-000000000071",
    "Close Grip Bench Press": "00000000-0000-0000-0001-000000000041",
    "Close Grip Lat Pull-Downs": "00000000-0000-0000-0001-000000000073",
    "Close Grip Seated Rows": "00000000-0000-0000-0001-000000000075",
    "Close Grip Upright Rows": "00000000-0000-0000-0001-000000000079",
    "Concentration Curls": "00000000-0000-0000-0001-000000000011",
    "Crunches": "00000000-0000-0000-0001-000000000112",
    "Deadlifts": "00000000-0000-0000-0001-000000000001",
    "Decline Bench Press": "00000000-0000-0000-0001-000000000062",
    "Donkey Calf Raises": "00000000-0000-0000-0001-000000000102",
    "Dumbbell Bench Press": "00000000-0000-0000-0001-000000000065",
    "Dumbbell Curls": "00000000-0000-0000-0001-000000000010",
    "Dumbbell Flys": "00000000-0000-0000-0001-000000000033",
    "Dumbbell Lunges": "00000000-0000-0000-0001-000000000105",
    "Dumbbell Pullovers": "00000000-0000-0000-0001-000000000038",
    "Dumbbell Shrugs": "00000000-0000-0000-0001-000000000085",
    "Dumbbell Side Bends": "00000000-0000-0000-0001-000000000117",
    "Dumbbell Squats": "00000000-0000-0000-0001-000000000090",
    "EZ-Bar Curls": "00000000-0000-0000-0001-000000000121",
    "Face Pulls": "00000000-0000-0000-0001-000000000119",
    "Farmer's Carries": "00000000-0000-0000-0001-000000000130",
    "Finger Curls": "00000000-0000-0000-0001-000000000021",
    "Front Squats": "00000000-0000-0000-0001-000000000044",
    "Glute Bridges": "00000000-0000-0000-0001-000000000108",
    "Goblet Squats": "00000000-0000-0000-0001-000000000124",
    "Good Mornings": "00000000-0000-0000-0001-000000000098",
    "Hack Squats": "00000000-0000-0000-0001-000000000092",
    "Hammer Curls": "00000000-0000-0000-0001-000000000013",
    "Hanging Leg Raises": "00000000-0000-0000-0001-000000000046",
    "High Pulley Curls": "00000000-0000-0000-0001-000000000015",
    "High Pulley Lateral Extensions": "00000000-0000-0000-0001-000000000037",
    "High Pulley Neck Extensions": "00000000-0000-0000-0001-000000000089",
    "High Pulley Neck Pulls": "00000000-0000-0000-0001-000000000088",
    "Hip Thrusts": "00000000-0000-0000-0001-000000000123",
    "Incline Bench Press": "00000000-0000-0000-0001-000000000061",
    "Incline Dumbbell Curls": "00000000-0000-0000-0001-000000000012",
    "Incline Dumbbell Flys": "00000000-0000-0000-0001-000000000067",
    "Incline Dumbbell Presses": "00000000-0000-0000-0001-000000000066",
    "Landmine Press": "00000000-0000-0000-0001-000000000132",
    "Lat Pull-Downs": "00000000-0000-0000-0001-000000000072",
    "Lateral Raises": "00000000-0000-0000-0001-000000000032",
    "Leg Extensions": "00000000-0000-0000-0001-000000000095",
    "Leg Press": "00000000-0000-0000-0001-000000000093",
    "Leg Raises": "00000000-0000-0000-0001-000000000116",
    "Low Pulley Bent-Over Lateral Raises": "00000000-0000-0000-0001-000000000058",
    "Low Pulley Curls": "00000000-0000-0000-0001-000000000014",
    "Low Pulley Lateral Raises": "00000000-0000-0000-0001-000000000059",
    "Lying Barbell Tricep Extensions": "00000000-0000-0000-0001-000000000026",
    "Lying Dumbbell Tricep Extensions": "00000000-0000-0000-0001-000000000027",
    "Lying Leg Curls": "00000000-0000-0000-0001-000000000096",
    "Machine Back Extensions": "00000000-0000-0000-0001-000000000084",
    "Machine Bench Press": "00000000-0000-0000-0001-000000000063",
    "Machine Crunches": "00000000-0000-0000-0001-000000000115",
    "Machine Curls": "00000000-0000-0000-0001-000000000016",
    "Machine Hip Extensions": "00000000-0000-0000-0001-000000000107",
    "Machine Lateral Raises": "00000000-0000-0000-0001-000000000055",
    "Machine Shrugs": "00000000-0000-0000-0001-000000000087",
    "Machine Torso Rotations": "00000000-0000-0000-0001-000000000118",
    "One Dumbbell Front Raises": "00000000-0000-0000-0001-000000000060",
    "One-Arm Overhead Dumbbell Tricep Extensions": "00000000-0000-0000-0001-000000000028",
    "Overhead Press": "00000000-0000-0000-0001-000000000004",
    "Pallof Press": "00000000-0000-0000-0001-000000000127",
    "Pec Deck Flys": "00000000-0000-0000-0001-000000000068",
    "Pec Deck Rear Delt Laterals": "00000000-0000-0000-0001-000000000056",
    "Pendlay Rows": "00000000-0000-0000-0001-000000000120",
    "Power Cleans": "00000000-0000-0000-0001-000000000129",
    "Power Squats": "00000000-0000-0000-0001-000000000091",
    "Preacher Curls": "00000000-0000-0000-0001-000000000017",
    "Pull Ups": "00000000-0000-0000-0001-000000000006",
    "Pulley External Arm Rotations": "00000000-0000-0000-0001-000000000057",
    "Push-Ups": "00000000-0000-0000-0001-000000000064",
    "Rear Delt Flys": "00000000-0000-0000-0001-000000000042",
    "Reverse Barbell Curls": "00000000-0000-0000-0001-000000000022",
    "Reverse Tricep Pushdowns": "00000000-0000-0000-0001-000000000024",
    "Romanian Deadlifts": "00000000-0000-0000-0001-000000000009",
    "Seated Dumbbell Presses": "00000000-0000-0000-0001-000000000049",
    "Seated Dumbbell Tricep Extensions": "00000000-0000-0000-0001-000000000030",
    "Seated EZ-Bar Tricep Extensions": "00000000-0000-0000-0001-000000000031",
    "Seated Front Presses": "00000000-0000-0000-0001-000000000048",
    "Seated Leg Curls": "00000000-0000-0000-0001-000000000097",
    "Seated Machine Calf Raises": "00000000-0000-0000-0001-000000000103",
    "Seated Machine Hip Abductions": "00000000-0000-0000-0001-000000000111",
    "Seated Machine Hip Adductions": "00000000-0000-0000-0001-000000000100",
    "Seated Reverse Curls": "00000000-0000-0000-0001-000000000019",
    "Side Raises": "00000000-0000-0000-0001-000000000034",
    "Single Arm Dumbbell Rows": "00000000-0000-0000-0001-000000000077",
    "Sit-Ups": "00000000-0000-0000-0001-000000000113",
    "Spider Curls": "00000000-0000-0000-0001-000000000122",
    "Squats": "00000000-0000-0000-0001-000000000002",
    "Standing Cable Overhead Tricep Extensions": "00000000-0000-0000-0001-000000000025",
    "Standing Calf Raises": "00000000-0000-0000-0001-000000000039",
    "Standing Machine Calf Raises": "00000000-0000-0000-0001-000000000101",
    "Standing Machine Hip Abductions": "00000000-0000-0000-0001-000000000110",
    "Standing Reverse Curls": "00000000-0000-0000-0001-000000000018",
    "Step-Ups": "00000000-0000-0000-0001-000000000125",
    "Straight Arm Pull-Downs": "00000000-0000-0000-0001-000000000074",
    "Sumo Deadlifts": "00000000-0000-0000-0001-000000000082",
    "Supported T-Bar Rows": "00000000-0000-0000-0001-000000000081",
    "T-Bar Rows": "00000000-0000-0000-0001-000000000080",
    "Trap Bar Deadlifts": "00000000-0000-0000-0001-000000000083",
    "Trap Bar Shrugs": "00000000-0000-0000-0001-000000000086",
    "Tricep Kickbacks": "00000000-0000-0000-0001-000000000029",
    "Tricep Pushdowns": "00000000-0000-0000-0001-000000000023",
    "Upright Rows": "00000000-0000-0000-0001-000000000054",
    "Weighted Dips": "00000000-0000-0000-0001-000000000007",
    "Wide Grip Seated Rows": "00000000-0000-0000-0001-000000000076",
    "Wrist Curls": "00000000-0000-0000-0001-000000000020",
}

# Default exercise names that match what the iOS app creates (isCustom=False).
# These are the 5 fundamentals + Pull Ups + Weighted Dips + Barbell Curls.
DEFAULT_EXERCISE_NAMES = {
    "Deadlifts", "Squats", "Bench Press", "Overhead Press",
    "Barbell Rows", "Pull Ups", "Weighted Dips", "Barbell Curls",
}

# Icon mapping mirroring IconCarouselPicker.suggestedIcon(for:) on iOS.
# Exact name matches first, then keyword fallbacks. First match wins.
def suggested_icon(name: str) -> str:
    lowered = name.lower()

    # Exact / specific name matches (arms)
    exact_matches = {
        "dumbbell curls": "DumbbellCurlsIcon",
        "concentration curls": "ConcentrationCurlsIcon",
        "incline dumbbell curls": "InclineDumbbellCurlsIcon",
        "hammer curls": "HammerCurlsIcon",
        "low pulley curls": "LowPulleyCurlsIcon",
        "high pulley curls": "HighPulleyCurlsIcon",
        "machine curls": "MachineCurlsIcon",
        "preacher curls": "PreacherCurlsIcon",
        "standing reverse curls": "StandingReverseCurlsIcon",
        "seated reverse curls": "SeatedReverseCurlsIcon",
        "wrist curls": "WristCurlsIcon",
        "finger curls": "FingerCurlsIcon",
        "reverse barbell curls": "ReverseBarbellCurlsIcon",
        "tricep pushdowns": "TricepPushdownsIcon",
        "reverse tricep pushdowns": "ReverseTricepPushdownsIcon",
        "standing cable overhead tricep extensions": "StandingCableOverheadTricepExtensionsIcon",
        "lying barbell tricep extensions": "LyingBarbellTricepExtensionsIcon",
        "lying dumbbell tricep extensions": "LyingDumbbellTricepExtensionsIcon",
        "one-arm overhead dumbbell tricep extensions": "OneArmOverheadDumbbellTricepExtensionsIcon",
        "tricep kickbacks": "TricepKickbacksIcon",
        "seated dumbbell tricep extensions": "SeatedDumbbellTricepExtensionsIcon",
        "seated ez-bar tricep extensions": "SeatedEZBarTricepExtensionsIcon",
    }
    if lowered in exact_matches:
        return exact_matches[lowered]

    # Generic keyword matches
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
# Names match Exercise.builtInTemplates exactly. Load types and movement types
# also match. Custom exercises (not in builtInTemplates) use isCustom=True.
EXERCISE_CATALOG = [
    # ── Barbell — Push ──
    ("Bench Press",                "Barbell",                     "Push",  185, 265),
    ("Incline Bench Press",        "Barbell",                     "Push",  155, 225),
    ("Overhead Press",             "Barbell",                     "Push",  115, 165),
    ("Close Grip Bench Press",     "Barbell",                     "Push",  155, 225),
    ("Decline Bench Press",        "Barbell",                     "Push",  165, 240),
    ("Back Presses",               "Barbell",                     "Push",  120, 175),
    ("Seated Front Presses",       "Barbell",                     "Push",  105, 155),
    ("Barbell Front Raises",       "Barbell",                     "Push",  55,  85),
    ("Landmine Press",             "Barbell",                     "Push",  135, 205),
    # ── Barbell — Pull ──
    ("Barbell Rows",               "Barbell",                     "Pull",  155, 235),
    ("Pendlay Rows",               "Barbell",                     "Pull",  145, 215),
    ("T-Bar Rows",                 "Barbell",                     "Pull",  135, 205),
    ("Supported T-Bar Rows",       "Barbell",                     "Pull",  130, 195),
    ("Barbell Curls",              "Barbell",                     "Pull",  75,  110),
    ("Reverse Barbell Curls",      "Barbell",                     "Pull",  50,  80),
    ("EZ-Bar Curls",               "Barbell",                     "Pull",  65,  100),
    ("Barbell Shrugs",             "Barbell",                     "Pull",  185, 315),
    ("Upright Rows",               "Barbell",                     "Pull",  85,  135),
    ("Close Grip Upright Rows",    "Barbell",                     "Pull",  80,  125),
    ("Barbell Pullovers",          "Barbell",                     "Pull",  55,  85),
    ("Trap Bar Shrugs",            "Barbell",                     "Pull",  195, 325),
    # ── Barbell — Squat ──
    ("Squats",                     "Barbell",                     "Squat", 225, 365),
    ("Front Squats",               "Barbell",                     "Squat", 185, 295),
    ("Box Squats",                 "Barbell",                     "Squat", 205, 315),
    ("Barbell Lunges",             "Barbell",                     "Squat", 115, 185),
    # ── Barbell — Hinge ──
    ("Deadlifts",                  "Barbell",                     "Hinge", 275, 425),
    ("Romanian Deadlifts",         "Barbell",                     "Hinge", 205, 315),
    ("Sumo Deadlifts",             "Barbell",                     "Hinge", 265, 405),
    ("Trap Bar Deadlifts",         "Barbell",                     "Hinge", 265, 405),
    ("Good Mornings",              "Barbell",                     "Hinge", 135, 205),
    ("Hip Thrusts",                "Barbell",                     "Hinge", 225, 365),
    ("Glute Bridges",              "Barbell",                     "Hinge", 205, 335),
    ("Power Cleans",               "Barbell",                     "Hinge", 155, 225),
    # ── Barbell — Push (tricep extensions) ──
    ("Lying Barbell Tricep Extensions",  "Barbell",               "Push",  65,  100),
    ("Seated EZ-Bar Tricep Extensions",  "Barbell",               "Push",  55,  85),
    # ── Bodyweight + Single Load — Push ──
    ("Weighted Dips",              "Bodyweight + Single Load",    "Push",  45,  90),
    ("Push-Ups",                   "Bodyweight + Single Load",    "Push",  0,   45),
    # ── Bodyweight + Single Load — Pull ──
    ("Pull Ups",                   "Bodyweight + Single Load",    "Pull",  0,   45),
    ("Chin-Ups",                   "Bodyweight + Single Load",    "Pull",  0,   45),
    # ── Bodyweight + Single Load — Core ──
    ("Hanging Leg Raises",         "Bodyweight + Single Load",    "Core",  0,   25),
    ("Crunches",                   "Bodyweight + Single Load",    "Core",  0,   25),
    ("Sit-Ups",                    "Bodyweight + Single Load",    "Core",  0,   25),
    ("Leg Raises",                 "Bodyweight + Single Load",    "Core",  0,   25),
    ("Ab Wheel Rollouts",         "Bodyweight + Single Load",    "Core",  0,   25),
    # ── Single Load — Push ──
    ("Dumbbell Bench Press",       "Single Load",                 "Push",  70,  100),
    ("Incline Dumbbell Presses",   "Single Load",                 "Push",  60,  90),
    ("Machine Bench Press",        "Single Load",                 "Push",  140, 220),
    ("Seated Dumbbell Presses",    "Single Load",                 "Push",  50,  75),
    ("Arnold Presses",             "Single Load",                 "Push",  45,  70),
    ("Cable Flys",                 "Single Load",                 "Push",  30,  55),
    ("Lateral Raises",             "Single Load",                 "Push",  20,  35),
    ("Side Raises",                "Single Load",                 "Push",  18,  32),
    ("Machine Lateral Raises",     "Single Load",                 "Push",  60,  100),
    ("Alternating Front Raises",   "Single Load",                 "Push",  20,  35),
    ("One Dumbbell Front Raises",  "Single Load",                 "Push",  25,  40),
    ("Low Pulley Lateral Raises",  "Single Load",                 "Push",  15,  30),
    ("Cable Lateral Raises",       "Single Load",                 "Push",  15,  30),
    ("Tricep Pushdowns",           "Single Load",                 "Push",  50,  80),
    ("Reverse Tricep Pushdowns",   "Single Load",                 "Push",  35,  60),
    ("Standing Cable Overhead Tricep Extensions", "Single Load",  "Push",  40,  65),
    ("Lying Dumbbell Tricep Extensions", "Single Load",           "Push",  25,  45),
    ("One-Arm Overhead Dumbbell Tricep Extensions", "Single Load","Push",  20,  35),
    ("Tricep Kickbacks",           "Single Load",                 "Push",  15,  30),
    ("Seated Dumbbell Tricep Extensions", "Single Load",          "Push",  30,  50),
    ("Dumbbell Flys",              "Single Load",                 "Push",  30,  50),
    ("Incline Dumbbell Flys",      "Single Load",                 "Push",  25,  45),
    ("Pec Deck Flys",              "Single Load",                 "Push",  100, 160),
    ("High Pulley Neck Extensions","Single Load",                 "Push",  30,  50),
    # ── Single Load — Pull ──
    ("Cable Rows",                 "Single Load",                 "Pull",  120, 190),
    ("Close Grip Seated Rows",     "Single Load",                 "Pull",  110, 175),
    ("Wide Grip Seated Rows",      "Single Load",                 "Pull",  100, 165),
    ("Single Arm Dumbbell Rows",   "Single Load",                 "Pull",  70,  110),
    ("Bent Over Dumbbell Rows",    "Single Load",                 "Pull",  55,  90),
    ("Lat Pull-Downs",             "Single Load",                 "Pull",  130, 200),
    ("Close Grip Lat Pull-Downs",  "Single Load",                 "Pull",  120, 185),
    ("Straight Arm Pull-Downs",    "Single Load",                 "Pull",  50,  80),
    ("Face Pulls",                 "Single Load",                 "Pull",  40,  65),
    ("Dumbbell Curls",             "Single Load",                 "Pull",  30,  50),
    ("Concentration Curls",        "Single Load",                 "Pull",  20,  35),
    ("Incline Dumbbell Curls",     "Single Load",                 "Pull",  20,  35),
    ("Hammer Curls",               "Single Load",                 "Pull",  35,  55),
    ("Low Pulley Curls",           "Single Load",                 "Pull",  30,  50),
    ("High Pulley Curls",          "Single Load",                 "Pull",  25,  40),
    ("Machine Curls",              "Single Load",                 "Pull",  40,  65),
    ("Preacher Curls",             "Single Load",                 "Pull",  35,  55),
    ("Standing Reverse Curls",     "Single Load",                 "Pull",  25,  40),
    ("Seated Reverse Curls",       "Single Load",                 "Pull",  20,  35),
    ("Finger Curls",               "Single Load",                 "Pull",  25,  45),
    ("Spider Curls",               "Single Load",                 "Pull",  20,  35),
    ("Wrist Curls",                "Single Load",                 "Pull",  30,  50),
    ("Dumbbell Pullovers",         "Single Load",                 "Pull",  40,  65),
    ("Rear Delt Flys",             "Single Load",                 "Pull",  20,  35),
    ("Cable Y Raises",             "Single Load",                 "Pull",  15,  30),
    ("Bent Over Lateral Raises",   "Single Load",                 "Pull",  15,  28),
    ("Pec Deck Rear Delt Laterals","Single Load",                 "Pull",  50,  85),
    ("Pulley External Arm Rotations", "Single Load",              "Pull",  15,  25),
    ("Low Pulley Bent-Over Lateral Raises", "Single Load",        "Pull",  15,  28),
    ("High Pulley Lateral Extensions", "Single Load",             "Pull",  25,  45),
    ("Dumbbell Shrugs",            "Single Load",                 "Pull",  70,  110),
    ("Machine Shrugs",             "Single Load",                 "Pull",  120, 190),
    ("High Pulley Neck Pulls",     "Single Load",                 "Pull",  30,  50),
    # ── Single Load — Squat ──
    ("Bulgarian Split Squats",     "Single Load",                 "Squat", 50,  80),
    ("Goblet Squats",              "Single Load",                 "Squat", 60,  100),
    ("Leg Press",                  "Single Load",                 "Squat", 360, 580),
    ("Leg Extensions",             "Single Load",                 "Squat", 100, 160),
    ("Dumbbell Squats",            "Single Load",                 "Squat", 55,  90),
    ("Power Squats",               "Single Load",                 "Squat", 180, 280),
    ("Hack Squats",                "Single Load",                 "Squat", 180, 280),
    ("Dumbbell Lunges",            "Single Load",                 "Squat", 50,  80),
    ("Step-Ups",                   "Single Load",                 "Squat", 40,  70),
    # ── Single Load — Hinge ──
    ("Back Extensions",            "Single Load",                 "Hinge", 25,  50),
    ("Machine Back Extensions",    "Single Load",                 "Hinge", 70,  120),
    ("Lying Leg Curls",            "Single Load",                 "Hinge", 80,  130),
    ("Seated Leg Curls",           "Single Load",                 "Hinge", 80,  130),
    ("Machine Hip Extensions",     "Single Load",                 "Hinge", 80,  130),
    ("Standing Calf Raises",       "Single Load",                 "Other", 70,  120),
    # ── Single Load — Core ──
    ("Cable Crunches",             "Single Load",                 "Core",  80,  130),
    ("Machine Crunches",           "Single Load",                 "Core",  60,  100),
    ("Dumbbell Side Bends",        "Single Load",                 "Core",  50,  80),
    ("Cable Woodchops",            "Single Load",                 "Core",  40,  65),
    ("Machine Torso Rotations",    "Single Load",                 "Core",  40,  65),
    ("Pallof Press",               "Single Load",                 "Core",  30,  50),
    # ── Single Load — Other ──
    ("Standing Machine Calf Raises", "Single Load",               "Other", 160, 260),
    ("Donkey Calf Raises",         "Single Load",                 "Other", 160, 260),
    ("Seated Machine Calf Raises", "Single Load",                 "Other", 100, 170),
    ("Cable Kickbacks",            "Single Load",                 "Other", 25,  45),
    ("Cable Hip Adductions",       "Single Load",                 "Other", 30,  50),
    ("Seated Machine Hip Adductions", "Single Load",              "Other", 80,  130),
    ("Cable Hip Abductions",       "Single Load",                 "Other", 30,  50),
    ("Standing Machine Hip Abductions", "Single Load",            "Other", 70,  110),
    ("Seated Machine Hip Abductions", "Single Load",              "Other", 70,  110),
    ("Farmer's Carries",           "Single Load",                 "Other", 70,  110),
]


# ─── Set Plans ───────────────────────────────────────────────────────────────

# Matches SetPlan.builtInPlans exactly (16 plans with deterministic UUIDs)
SET_PLANS = [
    ("00000000-0000-0000-0000-000000000101", "Standard",            ["easy", "easy", "moderate", "moderate", "hard", "pr"],                        "Progressive warmup to PR attempt"),
    ("00000000-0000-0000-0000-000000000102", "Grease the Groove",   ["easy", "easy", "easy", "easy", "moderate", "moderate", "moderate", "hard"],  "High volume, low intensity"),
    ("00000000-0000-0000-0000-000000000103", "Maintenance",         ["moderate", "moderate", "hard"],                                              "Moderate volume, hold strength"),
    ("00000000-0000-0000-0000-000000000104", "Deload",              ["easy", "easy", "easy"],                                                      "Recovery phase"),
    ("00000000-0000-0000-0000-000000000105", "Pyramid",             ["easy", "moderate", "hard", "pr", "hard", "moderate"],                        "Build up then back off"),
    ("00000000-0000-0000-0000-000000000106", "Top Set + Backoff",   ["easy", "moderate", "hard", "pr", "moderate", "moderate"],                    "Work up to max, drop intensity"),
    ("00000000-0000-0000-0000-000000000107", "Reverse Pyramid",     ["hard", "pr", "hard", "moderate", "moderate", "easy"],                        "Heaviest set first, then reduce"),
    ("00000000-0000-0000-0000-000000000108", "Wave Loading",        ["moderate", "hard", "pr", "moderate", "hard", "pr"],                          "Ascending waves of intensity"),
    ("00000000-0000-0000-0000-000000000109", "Cluster Sets",        ["hard", "hard", "hard", "hard", "hard"],                                      "Short rest between heavy singles/doubles"),
    ("00000000-0000-0000-0000-000000000110", "Rest-Pause",          ["hard", "pr", "hard", "hard"],                                                "Near-failure set, brief rest, continue"),
    ("00000000-0000-0000-0000-000000000111", "Drop Sets",           ["pr", "hard", "moderate", "easy"],                                            "Reduce weight each set, rep to failure"),
    ("00000000-0000-0000-0000-000000000112", "Ladders",             ["easy", "easy", "moderate", "moderate", "hard", "moderate", "hard", "pr"],     "Ascending rep ladder pattern"),
    ("00000000-0000-0000-0000-000000000113", "Pause Reps",          ["moderate", "moderate", "hard", "hard"],                                      "Paused reps to build positional strength"),
    ("00000000-0000-0000-0000-000000000114", "Speed / Dynamic",     ["easy", "easy", "easy", "easy", "easy", "easy", "easy", "easy"],              "Submaximal weight, max velocity"),
    ("00000000-0000-0000-0000-000000000115", "EMOM",                ["moderate", "moderate", "moderate", "moderate", "moderate", "moderate"],       "Every minute on the minute"),
    ("00000000-0000-0000-0000-000000000116", "Technique",           ["easy", "easy", "easy", "moderate", "moderate"],                              "Light load, focus on form"),
]


# ─── Exercise Groups ─────────────────────────────────────────────────────────

# Matches ExerciseGroup.builtInTemplates exactly (6 groups with deterministic UUIDs).
# Exercise IDs reference the APP's built-in UUIDs (segment 0001).
EXERCISE_GROUPS = [
    {
        "groupId": "00000000-0000-0000-0002-000000000001",
        "name": "Strength Tier",
        "exerciseIds": [
            "00000000-0000-0000-0001-000000000001",  # Deadlifts
            "00000000-0000-0000-0001-000000000002",  # Squats
            "00000000-0000-0000-0001-000000000003",  # Bench Press
            "00000000-0000-0000-0001-000000000005",  # Barbell Rows
            "00000000-0000-0000-0001-000000000004",  # Overhead Press
        ],
        "sortOrder": 0,
    },
    {
        "groupId": "00000000-0000-0000-0002-000000000002",
        "name": "Deadlifts+",
        "exerciseIds": [
            "00000000-0000-0000-0001-000000000001",  # Deadlifts
            "00000000-0000-0000-0001-000000000044",  # Front Squats
            "00000000-0000-0000-0001-000000000045",  # Back Extensions
            "00000000-0000-0000-0001-000000000046",  # Hanging Leg Raises
        ],
        "sortOrder": 1,
    },
    {
        "groupId": "00000000-0000-0000-0002-000000000003",
        "name": "Squats+",
        "exerciseIds": [
            "00000000-0000-0000-0001-000000000002",  # Squats
            "00000000-0000-0000-0001-000000000035",  # Bulgarian Split Squats
            "00000000-0000-0000-0001-000000000009",  # Romanian Deadlifts
            "00000000-0000-0000-0001-000000000039",  # Standing Calf Raises
        ],
        "sortOrder": 2,
    },
    {
        "groupId": "00000000-0000-0000-0002-000000000004",
        "name": "Bench Press+",
        "exerciseIds": [
            "00000000-0000-0000-0001-000000000003",  # Bench Press
            "00000000-0000-0000-0001-000000000007",  # Weighted Dips
            "00000000-0000-0000-0001-000000000033",  # Dumbbell Flys
            "00000000-0000-0000-0001-000000000032",  # Lateral Raises
        ],
        "sortOrder": 3,
    },
    {
        "groupId": "00000000-0000-0000-0002-000000000005",
        "name": "Barbell Rows+",
        "exerciseIds": [
            "00000000-0000-0000-0001-000000000005",  # Barbell Rows
            "00000000-0000-0000-0001-000000000006",  # Pull Ups
            "00000000-0000-0000-0001-000000000008",  # Barbell Curls
            "00000000-0000-0000-0001-000000000038",  # Dumbbell Pullovers
        ],
        "sortOrder": 4,
    },
    {
        "groupId": "00000000-0000-0000-0002-000000000006",
        "name": "OHP+",
        "exerciseIds": [
            "00000000-0000-0000-0001-000000000004",  # Overhead Press
            "00000000-0000-0000-0001-000000000041",  # Close Grip Bench Press
            "00000000-0000-0000-0001-000000000042",  # Rear Delt Flys
            "00000000-0000-0000-0001-000000000043",  # Cable Y Raises
        ],
        "sortOrder": 5,
    },
]


_PLACEHOLDER_SEQ = [
    ("Full Body A", ["Squats", "Bench Press", "Barbell Rows", "Overhead Press", "Barbell Curls", "Cable Crunches"]),
    ("Full Body B", ["Deadlifts", "Incline Bench Press", "Lat Pull-Downs", "Seated Dumbbell Presses", "Hammer Curls", "Cable Woodchops"]),
    ("Full Body C", ["Front Squats", "Close Grip Bench Press", "Single Arm Dumbbell Rows", "Lateral Raises", "Lying Leg Curls", "Dumbbell Side Bends"]),
    # Push/Pull/Legs (Phase 2-3)
    ("Push A", ["Bench Press", "Overhead Press", "Weighted Dips", "Incline Dumbbell Presses", "Cable Flys", "Tricep Pushdowns", "Lateral Raises"]),
    ("Push B", ["Incline Bench Press", "Back Presses", "Machine Bench Press", "Standing Cable Overhead Tricep Extensions", "Lateral Raises", "Cable Flys"]),
    ("Pull A", ["Barbell Rows", "Pull Ups", "Face Pulls", "Barbell Curls", "Hammer Curls", "Barbell Shrugs"]),
    ("Pull B", ["Pendlay Rows", "Cable Rows", "Single Arm Dumbbell Rows", "Low Pulley Curls", "Dumbbell Curls", "Face Pulls"]),
    ("Legs Squat", ["Squats", "Front Squats", "Leg Press", "Leg Extensions", "Bulgarian Split Squats", "Standing Machine Calf Raises"]),
    ("Legs Hinge", ["Deadlifts", "Romanian Deadlifts", "Lying Leg Curls", "Hip Thrusts", "Back Extensions", "Standing Machine Calf Raises"]),
    # Specialization (Phase 3)
    ("Bench Specialization", ["Bench Press", "Close Grip Bench Press", "Decline Bench Press", "Dumbbell Bench Press", "Tricep Pushdowns"]),
    ("Squat Specialization", ["Squats", "Front Squats", "Box Squats", "Leg Press", "Leg Extensions"]),
    ("Deadlift Specialization", ["Deadlifts", "Sumo Deadlifts", "Romanian Deadlifts", "Barbell Rows", "Lying Leg Curls"]),
    ("OHP Specialization", ["Overhead Press", "Back Presses", "Landmine Press", "Seated Dumbbell Presses", "Lateral Raises"]),
    ("Back Specialization", ["Barbell Rows", "T-Bar Rows", "Close Grip Seated Rows", "Lat Pull-Downs", "Face Pulls"]),
    ("Arm Day", ["Barbell Curls", "Hammer Curls", "Low Pulley Curls", "Tricep Pushdowns", "Standing Cable Overhead Tricep Extensions"]),
    ("Olympic Lifting", ["Power Cleans", "Front Squats", "Overhead Press", "Barbell Rows", "Romanian Deadlifts"]),
    # Extra variety
    ("Upper Body A", ["Bench Press", "Barbell Rows", "Overhead Press", "Pull Ups", "Barbell Curls", "Weighted Dips"]),
    ("Upper Body B", ["Incline Bench Press", "Pendlay Rows", "Seated Dumbbell Presses", "Cable Rows", "Hammer Curls", "Cable Flys"]),
    ("Lower Body A", ["Squats", "Romanian Deadlifts", "Leg Press", "Lying Leg Curls", "Standing Machine Calf Raises"]),
    ("Lower Body B", ["Deadlifts", "Front Squats", "Bulgarian Split Squats", "Leg Extensions", "Standing Machine Calf Raises"]),
    ("Push Hypertrophy", ["Machine Bench Press", "Incline Dumbbell Presses", "Cable Flys", "Machine Lateral Raises", "Standing Cable Overhead Tricep Extensions"]),
    ("Pull Hypertrophy", ["Close Grip Seated Rows", "Lat Pull-Downs", "Cable Rows", "Face Pulls", "Dumbbell Curls", "Rear Delt Flys"]),
    ("Legs Hypertrophy", ["Leg Press", "Goblet Squats", "Leg Extensions", "Lying Leg Curls", "Standing Machine Calf Raises", "Back Extensions"]),
    ("Core Focus", ["Cable Crunches", "Cable Woodchops", "Dumbbell Side Bends", "Ab Wheel Rollouts", "Pallof Press"]),
    ("Posterior Chain", ["Deadlifts", "Good Mornings", "Hip Thrusts", "Lying Leg Curls", "Back Extensions"]),
    ("Full Body D", ["Squats", "Overhead Press", "Barbell Rows", "Romanian Deadlifts", "Barbell Curls"]),
    ("Full Body E", ["Deadlifts", "Bench Press", "Lat Pull-Downs", "Goblet Squats", "Cable Crunches"]),
    ("Push C", ["Close Grip Bench Press", "Seated Dumbbell Presses", "Machine Bench Press", "Lateral Raises", "Tricep Pushdowns"]),
    ("Pull C", ["T-Bar Rows", "Lat Pull-Downs", "Single Arm Dumbbell Rows", "Face Pulls", "Barbell Curls", "Dumbbell Shrugs"]),
    ("Legs C", ["Box Squats", "Romanian Deadlifts", "Leg Press", "Lying Leg Curls", "Dumbbell Lunges", "Standing Machine Calf Raises"]),
    ("Strength Test Day", ["Squats", "Bench Press", "Deadlifts"]),
    ("Accessory Day", ["Lateral Raises", "Face Pulls", "Hammer Curls", "Tricep Pushdowns", "Cable Crunches", "Standing Machine Calf Raises"]),
    ("Power Day", ["Power Cleans", "Back Presses", "Box Squats", "Barbell Rows"]),
    ("GPP Day", ["Back Extensions", "Goblet Squats", "Dumbbell Lunges", "Cable Woodchops", "Standing Machine Calf Raises"]),
    ("Pressing Focus", ["Bench Press", "Overhead Press", "Decline Bench Press", "Dumbbell Bench Press", "Seated Dumbbell Presses"]),
    ("Rowing Focus", ["Barbell Rows", "Pendlay Rows", "Cable Rows", "Close Grip Seated Rows", "Single Arm Dumbbell Rows"]),
    ("Leg Day Heavy", ["Squats", "Deadlifts", "Leg Press", "Hip Thrusts"]),
    ("Recovery Day", ["Cable Crunches", "Face Pulls", "Lateral Raises", "Wrist Curls", "Standing Machine Calf Raises"]),
    ("Volume Bench", ["Bench Press", "Incline Bench Press", "Close Grip Bench Press", "Dumbbell Bench Press"]),
    ("Volume Squat", ["Squats", "Front Squats", "Box Squats", "Goblet Squats"]),
    ("Push Pull A", ["Bench Press", "Barbell Rows", "Overhead Press", "Lat Pull-Downs", "Tricep Pushdowns", "Barbell Curls"]),
    ("Push Pull B", ["Incline Bench Press", "Cable Rows", "Seated Dumbbell Presses", "Face Pulls", "Cable Flys", "Hammer Curls"]),
    ("Athletic Day", ["Power Cleans", "Box Squats", "Back Presses", "Barbell Lunges", "Step-Ups", "Back Extensions"]),
    ("Isolation Focus", ["Lateral Raises", "Rear Delt Flys", "Leg Extensions", "Lying Leg Curls", "Dumbbell Curls", "Tricep Pushdowns"]),
    ("Compound Only", ["Squats", "Bench Press", "Deadlifts", "Overhead Press", "Barbell Rows"]),
    ("Dumbbell Day", ["Dumbbell Bench Press", "Single Arm Dumbbell Rows", "Seated Dumbbell Presses", "Romanian Deadlifts", "Dumbbell Curls", "Dumbbell Lunges"]),
    ("Machine Day", ["Machine Bench Press", "Close Grip Seated Rows", "Leg Press", "Leg Extensions", "Lying Leg Curls", "Standing Machine Calf Raises"]),
    ("Cable Day", ["Cable Flys", "Cable Rows", "Low Pulley Curls", "Tricep Pushdowns", "Cable Crunches", "Back Extensions"]),
    ("Barbell Complex", ["Power Cleans", "Front Squats", "Overhead Press", "Barbell Rows", "Romanian Deadlifts"]),
    ("Weak Point Day", ["Front Squats", "Decline Bench Press", "Pendlay Rows", "Good Mornings", "Cable Crunches", "Face Pulls"]),
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
    """Generate exercise items from the catalog.

    Uses the app's built-in UUIDs for exercises that match the iOS catalog,
    so lift sets and e1RMs reference the same IDs the app expects.
    """
    exercises = []
    exercise_map = {}  # name -> exerciseItemId

    created = training_start - timedelta(days=1)

    for i, (name, load_type, movement_type, base_1rm, peak_1rm) in enumerate(EXERCISE_CATALOG):
        # Use the app's built-in UUID if this exercise exists in the catalog
        eid = BUILTIN_EXERCISE_UUIDS.get(name, det_uuid(f"exercise-{i}-{name}"))
        exercise_map[name] = {
            "exerciseItemId": eid,
            "loadType": load_type,
            "base_1rm": base_1rm,
            "peak_1rm": peak_1rm,
        }

        is_custom = name not in BUILTIN_EXERCISE_UUIDS
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
        })

    return exercises, exercise_map


def generate_training_calendar(training_start, training_end):
    """Generate training session dates across 3 phases.

    Phase boundaries scale proportionally to the total training window:
    Phase 1 (~first 15%): 3 days/week full-body
    Phase 2 (~next 35%): 5 days/week PPL
    Phase 3 (remainder): 5-6 days/week PPL + specialization
    """
    sessions = []
    current = training_start

    total_weeks = max(1, (training_end - training_start).days // 7)
    phase1_end = max(4, total_weeks // 6)
    phase2_end = max(phase1_end + 4, total_weeks // 2)

    week_num = 0
    while current < training_end:
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
            if session_date >= training_end:
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


def generate_lift_sets_and_e1rms(sessions, exercise_map, training_start, training_end):
    """Generate lift sets and corresponding estimated 1RMs.

    Each training day gets exactly 28 sets (16 on deload weeks), distributed
    evenly across the chosen exercises.
    """
    SETS_PER_DAY = 56
    DELOAD_SETS_PER_DAY = 28

    rng = random.Random(42)

    print(f"  Training sessions: {len(sessions)}")
    print(f"  Sets per day: {SETS_PER_DAY} (deload: {DELOAD_SETS_PER_DAY})")

    total_days = (training_end - training_start).days
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

                # Minimum weight depends on load type
                if load_type == "Bodyweight + Single Load":
                    weight = max(0.0, weight)
                elif load_type == "Single Load":
                    weight = max(5.0, weight)
                else:  # Barbell
                    weight = max(45.0, weight)

                # Vary reps slightly per set
                set_reps = max(1, reps + rng.randint(-1, 1))

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


def generate_set_plans():
    """Generate all 16 built-in set plans matching the app."""
    now = ts_z(datetime.now())
    set_plans = []

    for plan_id, name, sequence, description in SET_PLANS:
        set_plans.append({
            "userId": POWER_USER_ID,
            "planId": plan_id,
            "name": name,
            "effortSequence": sequence,
            "planDescription": description,
            "isCustom": False,
            "createdTimezone": TIMEZONE,
            "createdDatetime": now,
            "lastModifiedDatetime": now,
        })

    return set_plans


def generate_exercise_groups():
    """Generate all 6 built-in exercise groups matching the app."""
    now = ts_z(datetime.now())
    groups = []

    for group_def in EXERCISE_GROUPS:
        groups.append({
            "userId": POWER_USER_ID,
            "groupId": group_def["groupId"],
            "name": group_def["name"],
            "exerciseIds": group_def["exerciseIds"],
            "sortOrder": group_def["sortOrder"],
            "isCustom": False,
            "createdTimezone": TIMEZONE,
            "createdDatetime": now,
            "lastModifiedDatetime": now,
            "deleted": False,
            "pendingSync": False,
        })

    return groups


def generate_static_records(training_end):
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
        "biologicalSex": "male",
        "hasMetStrengthTierConditions": True,
        "activeSetPlanId": "00000000-0000-0000-0000-000000000101",
        "createdDatetime": created_str,
        "lastModifiedDatetime": created_str,
    }

    entitlement_end = training_end + timedelta(days=365)
    entitlement = {
        "userId": POWER_USER_ID,
        "startUtc": "2024-02-20T00:00:00Z",
        "endUtc": ts_z(entitlement_end),
        "entitlementName": "com.weightapp.premium.annual",
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
    parser.add_argument(
        "--stale",
        type=int,
        default=0,
        help="Months of staleness — shifts training end date back by N months (default: 0, meaning data ends today)",
    )
    args = parser.parse_args()

    training_end = TRAINING_END - timedelta(days=args.stale * 30)
    training_start = training_end - timedelta(days=args.months * 30)

    print(f"Generating power user data ({args.months} months of history)...")
    print(f"  User: {EMAIL} ({POWER_USER_ID})")
    print(f"  Training window: {training_start.date()} to {training_end.date()} ({args.months * 30} days)")
    if args.stale > 0:
        print(f"  Stale period: last {args.stale} months have no data")
    print()

    # Seed for deterministic exercise selection in sessions
    random.seed(42)

    # Generate all data
    print("Generating exercises...")
    exercises, exercise_map = generate_exercises(training_start)
    print(f"  {len(exercises)} exercises")

    print("Generating training calendar...")
    sessions = generate_training_calendar(training_start, training_end)
    print(f"  {len(sessions)} training sessions")

    print("Generating lift sets and estimated 1RMs...")
    lift_sets, e1rms = generate_lift_sets_and_e1rms(sessions, exercise_map, training_start, training_end)
    print(f"  {len(lift_sets)} lift sets")
    print(f"  {len(e1rms)} estimated 1RMs")

    # Create a "stale exercise" test case: Incline Bench Press has lots of data
    # but nothing in the last 4 months. Tests that currentE1RMLocalCache fallback works.
    stale_exercise_name = "Incline Bench Press"
    if stale_exercise_name in exercise_map:
        stale_eid = exercise_map[stale_exercise_name]["exerciseItemId"]
        four_months_ago = training_end - timedelta(days=120)
        before = len(lift_sets)
        lift_sets = [s for s in lift_sets if not (s["exerciseId"] == stale_eid and
                     datetime.fromisoformat(s["createdDatetime"].replace("Z", "")) >= four_months_ago)]
        e1rms = [e for e in e1rms if not (e["exerciseId"] == stale_eid and
                 datetime.fromisoformat(e["createdDatetime"].replace("Z", "")) >= four_months_ago)]
        removed = before - len(lift_sets)
        print(f"  Removed {removed} recent sets for '{stale_exercise_name}' (stale exercise test)")

    print("Generating set plans...")
    set_plans = generate_set_plans()
    print(f"  {len(set_plans)} set plans")

    print("Generating exercise groups...")
    exercise_groups = generate_exercise_groups()
    print(f"  {len(exercise_groups)} exercise groups")

    print("Generating static records...")
    user, user_props, entitlement, sub_event = generate_static_records(training_end)

    print()
    print("=" * 60)
    print(f"Summary: {len(exercises)} exercises, "
          f"{len(lift_sets)} lift sets, {len(e1rms)} estimated 1RMs, "
          f"{len(set_plans)} set plans, {len(exercise_groups)} exercise groups")
    print("=" * 60)
    print()

    # Write to DynamoDB
    print("Writing to DynamoDB staging tables...")
    write_items("users", [user], "user record")
    write_items("user-properties", [user_props], "user properties")
    write_items("exercises", exercises, "exercises")
    write_items("lift-sets", lift_sets, "lift sets")
    write_items("estimated-1rm", e1rms, "estimated 1RMs")
    write_items("set-plans", set_plans, "set plans")
    # exercise-groups are client-side only (seeded by SeedService into SwiftData)
    write_items("entitlement-grants", [entitlement], "entitlement grant")
    write_items("subscription-events", [sub_event], "subscription event")

    print()
    print("Done! Power user data loaded into staging.")


if __name__ == "__main__":
    main()
