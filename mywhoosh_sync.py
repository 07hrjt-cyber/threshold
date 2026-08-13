#!/usr/bin/env python3
"""
mywhoosh_sync.py — Pull ride data from MyWhoosh and produce a JSON file
ready to load into THRESHOLD (same format the Garmin/Strava syncs use).

MyWhoosh has no official public developer API — this uses the
community-reverse-engineered one documented at
https://github.com/mywhoosh-community/mywhoosh-api, which logs in with
your regular MyWhoosh email/password (same as the app does).

IMPORTANT CAVEAT — read this before relying on it
---------------------------------------------------
Unlike the Garmin/Strava scripts, the endpoint this uses for historical
data (/player/player-distance) has its response *shape* undocumented in
the community docs — the example shown is just an empty array, so the
actual field names inside each day's record aren't confirmed. This
script tries several plausible field name guesses defensively. If none
of them match what your account actually returns, it will:
  1. Print a warning rather than silently writing wrong/empty data
  2. Save the raw, unparsed API response to mywhoosh_raw_debug.json
If that happens, send me that debug file and I'll fix the field
mapping in one round rather than you having to guess at it.

Also note: this appears to return day-level aggregates, not individual
ride records — so if you ride twice in one day, this will likely
produce a single combined entry for that day rather than two separate
workouts.

USAGE
-----
  python mywhoosh_sync.py --days 30

Requires MYWHOOSH_EMAIL and MYWHOOSH_PASSWORD as environment variables
(or it'll prompt interactively if run locally with a terminal attached).
"""

import argparse
import getpass
import json
import os
import sys
import uuid
from datetime import date, timedelta

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:\n    pip install -r requirements.txt\n")

PUBLIC_BASE = "https://services.mywhoosh.com"
MAIN_BASE = "https://services.mywhoosh.com/http-service/v1"


def login(email, password):
    resp = requests.post(f"{PUBLIC_BASE}/http-service/api/login", json={
        "Username": email,
        "Password": password,
        "Platform": "Android",
        "Action": 1001,
        "CorrelationId": str(uuid.uuid4()),
        "DeviceId": str(uuid.uuid4()),
        "Authorization": "",
    })
    if resp.status_code != 200:
        sys.exit(f"Login failed (HTTP {resp.status_code}): {resp.text}")
    data = resp.json()
    if not data.get("Success"):
        sys.exit(f"Login failed: {data.get('Message', data)}")
    return data["AccessToken"], data.get("WhooshId")


def get_credentials():
    email = os.environ.get("MYWHOOSH_EMAIL")
    password = os.environ.get("MYWHOOSH_PASSWORD")
    non_interactive = not sys.stdin.isatty()
    if not email or not password:
        if non_interactive:
            missing = [n for n, v in [("MYWHOOSH_EMAIL", email), ("MYWHOOSH_PASSWORD", password)] if not v]
            sys.exit(
                "Missing " + ", ".join(missing) + " while running non-interactively "
                "(e.g. GitHub Actions). Set these as repository secrets."
            )
        if not email:
            email = input("MyWhoosh email: ").strip()
        if not password:
            password = getpass.getpass("MyWhoosh password: ")
    return email, password


def seconds_to_hms(total_seconds):
    if not total_seconds:
        return ""
    total_seconds = int(round(total_seconds))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def km_to_display(km, imperial):
    if km is None:
        return None
    return round(km * 0.621371, 2) if imperial else round(km, 2)


# Plausible field name variants to try, in order, since the real shape
# isn't documented. Each tuple is (candidate keys) tried in order.
DATE_KEYS = ["date", "Date", "day", "Day", "rideDate", "RideDate", "activityDate"]
DISTANCE_KM_KEYS = ["distance", "Distance", "distanceKm", "DistanceKm", "totalDistance", "TotalDistance", "km", "Km"]
DURATION_SEC_KEYS = ["duration", "Duration", "durationSeconds", "DurationSeconds", "time", "Time", "rideTime", "RideTime", "totalTime", "TotalTime"]
CALORIES_KEYS = ["calories", "Calories", "totalCalories", "TotalCalories"]
ELEVATION_KEYS = ["elevation", "Elevation", "totalElevation", "TotalElevation"]


def first_present(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return None


def parse_day_stats(raw, imperial):
    """Returns dict of {date_str: workout_dict}. Defensive against
    undocumented field names — see module docstring."""
    records = None
    if isinstance(raw, dict):
        for key in ["DayBasedStats", "dayBasedStats", "data", "Data"]:
            if key in raw and isinstance(raw[key], list):
                records = raw[key]
                break
    elif isinstance(raw, list):
        records = raw

    if not records:
        return {}, False  # (calendar, matched_anything)

    calendar = {}
    matched_any = False
    for rec in records:
        date_val = first_present(rec, DATE_KEYS)
        if not date_val:
            continue
        # date_val might be an epoch int/str or an ISO date string
        try:
            if isinstance(date_val, (int, float)) or (isinstance(date_val, str) and date_val.isdigit()):
                day_str = date.fromtimestamp(int(date_val)).isoformat()
            else:
                day_str = str(date_val)[:10]
        except Exception:
            continue

        distance_km = first_present(rec, DISTANCE_KM_KEYS)
        duration_sec = first_present(rec, DURATION_SEC_KEYS)
        calories = first_present(rec, CALORIES_KEYS)
        elevation = first_present(rec, ELEVATION_KEYS)

        if distance_km is None and duration_sec is None:
            continue  # nothing usable in this record

        matched_any = True
        distance_val = km_to_display(float(distance_km), imperial) if distance_km is not None else ""
        duration_hms = seconds_to_hms(duration_sec) if duration_sec is not None else ""

        workout = {
            "id": f"mywhoosh-{day_str}",
            "sport": "bike",
            "title": "MyWhoosh Ride",
            "duration": duration_hms,
            "plannedDuration": duration_hms,
            "completedDuration": duration_hms,
            "plannedDistance": distance_val,
            "completedDistance": distance_val,
            "plannedCalories": calories or "",
            "completedCalories": calories or "",
            "autoCalc": True,  # let THRESHOLD calculate pace/speed from duration+distance
            "description": "",
            "notes": f"Elevation: {elevation}m" if elevation else "",
            "comments": "",
            "tags": "mywhoosh-sync",
            "equipment": "",
        }
        calendar[day_str] = [workout]

    return calendar, matched_any


def main():
    parser = argparse.ArgumentParser(description="Sync MyWhoosh rides into a THRESHOLD-importable JSON file.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--units", choices=["metric", "imperial"], default="metric")
    parser.add_argument("--output", type=str, default="threshold-mywhoosh-import.json")
    args = parser.parse_args()

    email, password = get_credentials()
    imperial = args.units == "imperial"

    print("Logging into MyWhoosh...")
    access_token, whoosh_id = login(email, password)
    print(f"Logged in (WhooshId: {whoosh_id})")

    headers = {"Authorization": f"Bearer {access_token}"}
    print(f"Fetching last {args.days} days of stats...")
    resp = requests.get(f"{MAIN_BASE}/player/player-distance", headers=headers, params={"days": args.days})
    if resp.status_code != 200:
        sys.exit(f"Failed to fetch player-distance (HTTP {resp.status_code}): {resp.text}")

    raw = resp.json()
    calendar, matched_any = parse_day_stats(raw, imperial)

    if not matched_any:
        debug_path = "mywhoosh_raw_debug.json"
        with open(debug_path, "w") as f:
            json.dump(raw, f, indent=2)
        print(f"\nWARNING: Couldn't confidently parse any day records from the "
              f"response — the field names didn't match my guesses (see the "
              f"module docstring for why). Raw response saved to {debug_path}.")
        print("No ride data written this run. Send me that debug file and I'll fix the parser.")
        # Still write an empty-but-valid output so nothing downstream breaks
        calendar = {}

    output = {"calendar": calendar}
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {args.output} — {len(calendar)} day(s) of ride data.")


if __name__ == "__main__":
    main()
