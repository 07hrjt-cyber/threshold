#!/usr/bin/env python3
"""
garmin_sync.py — Pull data from Garmin Connect and produce a JSON file
ready to load into THRESHOLD via its "Import" button.

This uses the unofficial `garminconnect` library, which authenticates the
same way the Garmin Connect mobile app does (your own login, not Garmin's
official partner API). That means:
  - It works today, no approval wait.
  - It can break if Garmin changes their login flow — if that happens,
    `pip install --upgrade garminconnect garth` is usually the fix.
  - It's for personal use with your own account, not a redistributable
    integration.

SETUP
-----
1. pip install -r requirements.txt
2. Run the script (see examples below). On first run it'll ask for your
   Garmin email/password interactively (or read them from environment
   variables — see below) and will cache a login session in
   ~/.garminconnect so you don't have to log in every time.

USAGE
-----
  python garmin_sync.py --days 30
  python garmin_sync.py --start 2026-07-01 --end 2026-08-05
  python garmin_sync.py --days 14 --units imperial --output my-sync.json

Then in THRESHOLD: click the avatar-adjacent "Import" button in the top
bar and select the generated JSON file. Re-running this script and
re-importing is safe — THRESHOLD merges by date/id rather than wiping
existing data.

ENVIRONMENT VARIABLES (optional, avoids interactive prompts)
--------------------------------------------------------------
  GARMIN_EMAIL
  GARMIN_PASSWORD
"""

import argparse
import getpass
import json
import os
import sys
from datetime import date, timedelta

try:
    from garminconnect import Garmin
except ImportError:
    sys.exit(
        "Missing dependency. Run:\n"
        "    pip install -r requirements.txt\n"
    )

TOKEN_DIR = os.path.expanduser("~/.garminconnect")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def login():
    """Log into Garmin Connect, reusing a cached session token if present."""
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    # Try cached session first (garminconnect stores tokens via garth).
    try:
        client = Garmin()
        client.login(TOKEN_DIR)
        print("Logged in using cached session.")
        return client
    except Exception:
        pass

    if not email:
        email = input("Garmin email: ").strip()
    if not password:
        password = getpass.getpass("Garmin password: ")

    client = Garmin(email, password)
    client.login()

    os.makedirs(TOKEN_DIR, exist_ok=True)
    try:
        client.garth.dump(TOKEN_DIR)
        print(f"Session cached to {TOKEN_DIR} for next time.")
    except Exception as e:
        print(f"(Could not cache session: {e})")

    return client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
SPORT_MAP = {
    "running": "run",
    "trail_running": "run",
    "treadmill_running": "run",
    "track_running": "run",
    "cycling": "bike",
    "road_biking": "bike",
    "mountain_biking": "bike",
    "indoor_cycling": "bike",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "strength_training": "other",
    "cardio_training": "other",
    "fitness_equipment": "other",
    "yoga": "other",
    "walking": "other",
}


def map_sport(activity_type_key):
    return SPORT_MAP.get((activity_type_key or "").lower(), "other")


def seconds_to_hms(total_seconds):
    if not total_seconds:
        return ""
    total_seconds = int(round(total_seconds))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def meters_to_distance(meters, imperial):
    if not meters:
        return None
    km = meters / 1000
    return round(km * 0.621371, 2) if imperial else round(km, 2)


def format_pace_or_speed(sport, duration_seconds, distance_value, imperial):
    """distance_value is already in km or mi depending on `imperial`."""
    if not duration_seconds or not distance_value:
        return ""
    if sport == "bike":
        hours = duration_seconds / 3600
        speed = distance_value / hours if hours else 0
        return f"{speed:.1f}"
    if sport == "swim":
        # distance_value is km/mi; convert to 100m/100yd segments
        segments = (distance_value * 1000 / 100) if not imperial else (distance_value * 1760 / 100)
        if not segments:
            return ""
        pace_sec = duration_seconds / segments
        m, s = divmod(int(round(pace_sec)), 60)
        return f"{m}:{s:02d}"
    # run / other -> pace per km or mi
    pace_sec = duration_seconds / distance_value
    m, s = divmod(int(round(pace_sec)), 60)
    return f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# Pull
# --------------------------------------------------------------------------
def pull_daily_metrics(client, day_str):
    """Returns dict of {sleepTime, sleepScore, recovery, readiness} for a day, values may be None."""
    out = {"sleepTime": None, "sleepScore": None, "recovery": None, "readiness": None}

    try:
        sleep = client.get_sleep_data(day_str)
        summary = (sleep or {}).get("dailySleepDTO", {}) or {}
        seconds = summary.get("sleepTimeSeconds")
        if seconds:
            out["sleepTime"] = round(seconds / 3600, 1)
        score = (summary.get("sleepScores") or {}).get("overall", {}).get("value")
        if score is not None:
            out["sleepScore"] = score
    except Exception as e:
        print(f"  [sleep] {day_str}: {e}")

    # Body Battery as a recovery proxy — Garmin doesn't expose a single
    # "recovery score" like some other platforms; body battery (0-100,
    # charge level) is the closest analogue.
    try:
        bb = client.get_body_battery(day_str, day_str)
        if bb and isinstance(bb, list) and bb[0].get("bodyBatteryValuesArray"):
            values = [v[1] for v in bb[0]["bodyBatteryValuesArray"] if v[1] is not None]
            if values:
                out["recovery"] = max(values)  # peak battery that day
    except Exception as e:
        print(f"  [body battery] {day_str}: {e}")

    # Training Readiness — not present in all garminconnect versions/devices.
    try:
        tr = client.get_training_readiness(day_str)
        if tr and isinstance(tr, list) and tr[0].get("score") is not None:
            out["readiness"] = tr[0]["score"]
    except Exception as e:
        print(f"  [training readiness] {day_str}: {e} (may not be supported for your device/library version)")

    return out


def pull_activities(client, start_str, end_str, imperial):
    """Returns dict of {date: [workout, ...]} for the range."""
    calendar = {}
    try:
        activities = client.get_activities_by_date(start_str, end_str)
    except Exception as e:
        print(f"  [activities] {e}")
        return calendar

    for act in activities or []:
        try:
            start_local = act.get("startTimeLocal", "")
            day_str = start_local.split(" ")[0].split("T")[0]
            if not day_str:
                continue

            sport = map_sport((act.get("activityType") or {}).get("typeKey"))
            duration_sec = act.get("duration")
            distance_m = act.get("distance")
            distance_val = meters_to_distance(distance_m, imperial)
            duration_hms = seconds_to_hms(duration_sec)
            pace = format_pace_or_speed(sport, duration_sec, distance_val, imperial)

            workout = {
                "id": f"garmin-{act.get('activityId')}",
                "sport": sport,
                "title": act.get("activityName") or "",
                "duration": duration_hms,
                "plannedDuration": duration_hms,
                "completedDuration": duration_hms,
                "plannedDistance": distance_val if distance_val is not None else "",
                "completedDistance": distance_val if distance_val is not None else "",
                "plannedPace": pace,
                "completedPace": pace,
                "plannedCalories": act.get("calories") or "",
                "completedCalories": act.get("calories") or "",
                "plannedTSS": act.get("activityTrainingLoad") or "",
                "completedTSS": act.get("activityTrainingLoad") or "",
                "hrAvg": act.get("averageHR") or "",
                "hrMax": act.get("maxHR") or "",
                "autoCalc": False,
                "description": "",
                "notes": "",
                "comments": "",
                "tags": "garmin-sync",
                "equipment": "",
            }
            calendar.setdefault(day_str, []).append(workout)
        except Exception as e:
            print(f"  [activity parse] {e}")

    return calendar


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Sync Garmin Connect data into a THRESHOLD-importable JSON file.")
    parser.add_argument("--days", type=int, default=30, help="How many days back to pull (default 30). Ignored if --start/--end given.")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--units", choices=["metric", "imperial"], default="metric")
    parser.add_argument("--output", type=str, default="threshold-garmin-import.json")
    args = parser.parse_args()

    if args.start:
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end) if args.end else date.today()
    else:
        end_date = date.fromisoformat(args.end) if args.end else date.today()
        start_date = end_date - timedelta(days=args.days)

    imperial = args.units == "imperial"

    print(f"Logging into Garmin Connect...")
    client = login()

    print(f"Pulling {start_date} to {end_date}...")

    metrics = {
        "sleepTime": {"history": []},
        "sleepScore": {"history": []},
        "recovery": {"history": []},
        "readiness": {"history": []},
    }

    day = start_date
    while day <= end_date:
        day_str = day.isoformat()
        print(f"  {day_str}")
        daily = pull_daily_metrics(client, day_str)
        for key, value in daily.items():
            if value is not None:
                metrics[key]["history"].append({"date": day_str, "value": value})
        day += timedelta(days=1)

    calendar = pull_activities(client, start_date.isoformat(), end_date.isoformat(), imperial)

    output = {"metrics": metrics, "calendar": calendar}

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    total_sessions = sum(len(v) for v in calendar.values())
    print(f"\nDone. Wrote {args.output}")
    print(f"  Sleep entries:      {len(metrics['sleepTime']['history'])}")
    print(f"  Sleep score entries:{len(metrics['sleepScore']['history'])}")
    print(f"  Body Battery entries:{len(metrics['recovery']['history'])}")
    print(f"  Readiness entries:  {len(metrics['readiness']['history'])}")
    print(f"  Activities:         {total_sessions}")
    print(f"\nOpen THRESHOLD -> avatar icon area's Import button -> select {args.output}")


if __name__ == "__main__":
    main()
