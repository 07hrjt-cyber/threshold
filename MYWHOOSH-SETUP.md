# MyWhoosh Sync — Setup Guide

Free, no subscription needed — but genuinely less certain than the
Garmin or Strava syncs. Read this before relying on it.

## The honest situation

MyWhoosh has no official developer API. This uses a
community-reverse-engineered one
([mywhoosh-community/mywhoosh-api](https://github.com/mywhoosh-community/mywhoosh-api))
that logs in with your regular email/password, same as the mobile app
does. Two things follow from that:

1. **It could break** if MyWhoosh changes their login flow — same risk
   as the Garmin script.
2. **The historical-data endpoint's exact response format isn't
   documented.** The community docs show the endpoint exists
   (`/player/player-distance`) but the example response is just an
   empty array — nobody's published what the actual field names look
   like inside it. The script guesses several plausible names
   (`distance`, `Distance`, `totalDistance`, etc.) and tries them in
   order.

## What happens if the guesses are wrong

The script won't silently write empty or garbage data. If it can't
confidently match any fields, it will:

- Print a clear warning
- Save the raw, unparsed API response to `mywhoosh_raw_debug.json`
- Still write a valid (but empty) output file so nothing downstream breaks

**If that happens:** send me `mywhoosh_raw_debug.json` and I'll fix
the field mapping — should be quick once we can see the real shape.

## Setup

1. Add `mywhoosh_sync.py`, updated `requirements.txt`, and
   `.github/workflows/mywhoosh-sync.yml` to your repo (root level, same
   as the others)
2. **Settings → Secrets and variables → Actions** → add:
   - `MYWHOOSH_EMAIL`
   - `MYWHOOSH_PASSWORD`
3. **Actions → MyWhoosh Sync → Run workflow** to test

Check the run log either way — it'll tell you plainly whether it found
real data or hit the undocumented-field problem above.

## Known limitation: daily aggregates, not individual rides

Even once parsing works, this endpoint appears to return day-level
totals rather than a list of individual rides. If you ride twice in
one day, you'll likely see one combined entry rather than two separate
workouts — different from how the Garmin/Strava syncs work, which do
track individual activities. Not fixable without a documented
per-activity endpoint, which doesn't currently exist for MyWhoosh.

## Local testing

You can run it directly to see what happens before setting up the
scheduled Action:

```bash
pip install -r requirements.txt
python mywhoosh_sync.py --days 7
```

It'll prompt for your email/password interactively if you don't set
the environment variables first.
