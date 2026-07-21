#!/usr/bin/env python3
"""
Emerald Isle (foreUP course 18977 / schedule 704) tee time tracker.

Snapshots which tee times are currently OPEN for Tue Jul 21 - Sun Jul 26 2026
and appends each snapshot to snapshots.csv.

The site never publishes what's BOOKED -- booked slots just vanish. So we
infer it from WHEN a slot vanished:

  - vanished right at the booking cutoff, spots still full -> NOBODY BOOKED IT
  - spots count dropped at some point                      -> partially booked
  - vanished well before the cutoff                        -> fully booked

The cutoff isn't hardcoded; it's inferred from the data, since no slot can
survive past it. Whatever the shortest lead time is across all slots, that's
approximately the cutoff.

Usage:
    python3 teetimes.py probe     # debug: show raw API response
    python3 teetimes.py snap      # take one snapshot, append to CSV
    python3 teetimes.py report    # analyze everything collected so far
"""

import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta

import requests

COURSE_ID = 18977
SCHEDULE_ID = 704
DATES = ["07-21-2026", "07-22-2026", "07-23-2026",
         "07-24-2026", "07-25-2026", "07-26-2026"]

# --- FRINGE WINDOW -------------------------------------------------------
# Report shows only the first N and last N tee times of each day. Ranked by
# position, not clock time, so it follows the sheet as sunrise/sunset shift.
# Everything is still logged to the CSV -- widening this later costs nothing,
# just edit and re-run report against data you already have.
FRINGE_FIRST_N = 10
FRINGE_LAST_N = 10
# Set FRINGE_ONLY = False to see the whole day again.
FRINGE_ONLY = True

# The known shape of the tee sheet. Because this is pinned rather than
# inferred, a slot booked before we ever took a snapshot still counts toward
# the ranking -- and gets reported as booked instead of silently vanishing.
DAY_START = "06:30"
DAY_END = "17:50"
INTERVAL_MIN = 10

# Booking closes ~55-60 min before tee time, and an unsold slot drops off the
# listing right then. So a slot last seen inside that band expired unsold;
# one that vanished appreciably earlier was taken by someone.
# Set to None to go back to inferring it from the data.
CUTOFF_MIN = 60
CUTOFF_SLACK = 20      # widen the band to absorb sampling gaps
# -------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "snapshots.csv")
BOOKING_URL = f"https://foreupsoftware.com/index.php/booking/{COURSE_ID}/{SCHEDULE_ID}"
API_URL = "https://foreupsoftware.com/index.php/api/booking/times"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BOOKING_URL + "#/teetimes",
    "Api-Key": "no_limits",
}

FIELDS = ["taken_at", "play_date", "tee_time", "holes",
          "spots_open", "price", "course"]


# ---------------------------------------------------------------- fetching

def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(BOOKING_URL, timeout=20)
    except requests.RequestException as e:
        print(f"  ! warm-up request failed ({e}); continuing")
    return s


def candidate_booking_classes(session):
    """booking_class is course-specific. Scrape it, else try common values."""
    found = []
    try:
        html = session.get(BOOKING_URL, timeout=20).text
        for m in re.finditer(r'"booking_class(?:_id)?"\s*:\s*"?(\d+)"?', html):
            if m.group(1) not in found:
                found.append(m.group(1))
    except requests.RequestException:
        pass
    for fb in ["", "1", "0"]:
        if fb not in found:
            found.append(fb)
    return found


def fetch_day(session, mmddyyyy, booking_class):
    params = {
        "time": "all", "date": mmddyyyy, "holes": "all", "players": "0",
        "booking_class": booking_class, "schedule_id": SCHEDULE_ID,
        "schedule_ids[]": SCHEDULE_ID, "specials_only": "0",
        "api_key": "no_limits",
    }
    try:
        r = session.get(API_URL, params=params, timeout=25)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.text.strip():
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def resolve_booking_class(session):
    for bc in candidate_booking_classes(session):
        for d in DATES:
            if fetch_day(session, d, bc):
                return bc
    return None


# ---------------------------------------------------------------- commands

def snap():
    session = make_session()
    bc = resolve_booking_class(session)
    if bc is None:
        print("No tee times came back. Run `python3 teetimes.py probe`.")
        sys.exit(1)

    taken_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for d in DATES:
        times = fetch_day(session, d, bc) or []
        for t in times:
            rows.append({
                "taken_at": taken_at,
                "play_date": d,
                "tee_time": t.get("time", ""),
                "holes": t.get("holes", ""),
                "spots_open": t.get("available_spots", ""),
                "price": t.get("green_fee", t.get("guest_green_fee", "")),
                "course": t.get("course_name", ""),
            })

    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"{taken_at}  booking_class={bc!r}  {len(rows)} open slots logged")


def _parse_tee(s):
    """foreUP gives '2026-07-21 07:00'. Be forgiving about the format."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def _full_grid(play_date):
    """Every slot the course offers that day, booked or not."""
    d = datetime.strptime(play_date, "%m-%d-%Y")
    sh, sm = (int(x) for x in DAY_START.split(":"))
    eh, em = (int(x) for x in DAY_END.split(":"))
    cur = d.replace(hour=sh, minute=sm)
    last = d.replace(hour=eh, minute=em)
    out = []
    while cur <= last:
        out.append(cur)
        cur += timedelta(minutes=INTERVAL_MIN)
    return out


def _fringe_slots(play_date):
    """The first N and last N tee times of the day, from the pinned grid."""
    grid = _full_grid(play_date)
    if not FRINGE_ONLY:
        return grid
    picked = grid[:FRINGE_FIRST_N] + (grid[-FRINGE_LAST_N:] if FRINGE_LAST_N else [])
    seen, out = set(), []
    for t in picked:            # dedupe if the two ends overlap
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def report():
    if not os.path.exists(CSV_PATH):
        print("No snapshots yet. Run `python3 teetimes.py snap` first.")
        return
    with open(CSV_PATH) as f:
        rows = [r for r in csv.DictReader(f) if r.get("tee_time")]
    if not rows:
        print("snapshots.csv has no usable rows.")
        return

    snaps = sorted({r["taken_at"] for r in rows})
    latest = snaps[-1]

    # median gap between snapshots -> how precisely we can time a disappearance
    times = [datetime.fromisoformat(s) for s in snaps]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    interval = statistics.median(gaps) if gaps else 60.0

    print(f"{len(snaps)} snapshots  {snaps[0]} -> {latest}")
    print(f"sampling every ~{interval:.0f} min\n")

    # per-slot history
    hist = {}
    for r in rows:
        key = (r["play_date"], r["tee_time"])
        h = hist.setdefault(key, {"first_spots": None, "last_spots": None,
                                  "last_seen": None, "min_spots": None})
        sp = _as_int(r["spots_open"])
        if h["first_spots"] is None:
            h["first_spots"] = sp
        h["last_spots"] = sp
        h["last_seen"] = r["taken_at"]
        if sp is not None:
            h["min_spots"] = sp if h["min_spots"] is None else min(h["min_spots"], sp)

    # lead time (minutes before tee-off) at which each slot was last seen
    for key, h in hist.items():
        tee = _parse_tee(key[1])
        h["tee_dt"] = tee
        h["lead"] = ((tee - datetime.fromisoformat(h["last_seen"])).total_seconds() / 60
                     if tee else None)
        h["gone"] = h["last_seen"] != latest

    # Infer the cutoff: nothing survives past it, so the shortest observed
    # leads cluster there. 10th percentile is robust to a stray outlier.
    leads = sorted(h["lead"] for h in hist.values()
                   if h["gone"] and h["lead"] is not None and h["lead"] > 0)
    inferred = leads[max(0, int(len(leads) * 0.10) - 1)] if leads else None

    if CUTOFF_MIN is not None:
        cutoff = float(CUTOFF_MIN)
        print(f"booking cutoff: {cutoff:.0f} min before tee time (set manually)")
        if inferred is not None:
            # cross-check: if the data disagrees badly, the setting is suspect
            flag = "  <-- disagrees, check CUTOFF_MIN" if abs(inferred - cutoff) > 45 else ""
            print(f"(data suggests ~{inferred:.0f} min){flag}")
    elif inferred is not None:
        cutoff = inferred
        print(f"inferred booking cutoff: ~{cutoff:.0f} min before tee time")
        print(f"(shortest lead seen: {leads[0]:.0f} min)")
    else:
        cutoff = 0.0
        print("not enough disappearances yet to infer the cutoff")
    print()

    # A slot counts as "expired unsold" if last seen within the cutoff band.
    # The band has to absorb both the course's own slop and our sampling gaps.
    tol = CUTOFF_SLACK + interval * 1.5

    # index observed slots by their actual datetime so we can line them up
    # against the pinned grid
    by_dt = {}
    for (play_date, _), h in hist.items():
        if h["tee_dt"] is not None:
            by_dt[(play_date, h["tee_dt"])] = h

    if FRINGE_ONLY:
        print(f"sheet runs {DAY_START}-{DAY_END} every {INTERVAL_MIN} min; "
              f"showing first {FRINGE_FIRST_N} and last {FRINGE_LAST_N}")
        print("(edit FRINGE_ONLY at the top of this file to see all times)\n")

    never, partial, booked, open_still, unseen = [], [], [], [], []
    for play_date in sorted({d for d, _ in hist}):
        for tee_dt in _fringe_slots(play_date):
            key = (play_date, tee_dt.strftime("%Y-%m-%d %H:%M"))
            h = by_dt.get((play_date, tee_dt))
            if h is None:
                # On the sheet but never once seen open -> it was taken before
                # tracking began. Counted as booked; we just don't know when.
                booked.append((key, {"tee_dt": tee_dt, "first_spots": None,
                                     "last_spots": None, "lead": None,
                                     "gone": True}))
                continue
            touched = (h["min_spots"] is not None
                       and h["first_spots"] is not None
                       and h["min_spots"] < h["first_spots"])
            if not h["gone"]:
                open_still.append((key, h))
            elif touched and (h["last_spots"] or 0) > 0:
                partial.append((key, h))
            elif h["lead"] is not None and h["lead"] <= cutoff + tol:
                never.append((key, h))
            else:
                booked.append((key, h))

    def dump(title, items, note=""):
        print(f"\n{title}: {len(items)}{note}")
        cur = None
        for (d, t), h in items:
            if d != cur:
                cur = d
                lab = datetime.strptime(d, "%m-%d-%Y").strftime("%a %b %d")
                print(f"  {lab}")
            tee = h["tee_dt"].strftime("%-I:%M %p") if h["tee_dt"] else t
            extra = ""
            if h["first_spots"] is not None:
                extra = f"  [{h['last_spots']}/{h['first_spots']} spots]"
            if h["lead"] is not None and h["gone"]:
                extra += f"  gone {h['lead']:.0f}m out"
            print(f"    {tee}{extra}")

    dump("NEVER BOOKED (expired at the cutoff, still full)", never)
    dump("PARTIALLY BOOKED (some spots sold)", partial)
    dump("BOOKED", booked)
    dump("STILL OPEN (not resolved yet)", open_still)

    print(f"\nCaveat: with {interval:.0f}-min sampling, anything vanishing "
          f"within ~{tol:.0f} min of the cutoff can't be told apart from a "
          f"last-minute booking.")

    # ------------------------------------------------------------------
    # Day-by-day view: every fringe slot, in time order, with its verdict.
    # This is the answer to "which fringe times went unsold, per day."
    # ------------------------------------------------------------------
    LABELS = {"never": "NEVER BOOKED", "partial": "partial",
              "booked": "booked", "open": "not resolved yet"}
    status = {}
    for lst, tag in ((never, "never"), (partial, "partial"),
                     (booked, "booked"), (open_still, "open")):
        for key, h in lst:
            status[key] = tag

    print("\n\n" + "=" * 62)
    print("BY DAY")
    print("=" * 62)

    totals = {k: 0 for k in LABELS}
    per_day = {}
    for play_date in sorted({d for d, _ in hist}):
        slots = _fringe_slots(play_date)
        counts = {k: 0 for k in LABELS}
        lab = datetime.strptime(play_date, "%m-%d-%Y").strftime("%A %b %d")
        print(f"\n{lab}")
        prev_period = None
        for tee_dt in slots:
            key = (play_date, tee_dt.strftime("%Y-%m-%d %H:%M"))
            tag = status.get(key, "open")
            counts[tag] += 1
            totals[tag] += 1
            period = "morning" if tee_dt.hour < 12 else "evening"
            if period != prev_period:
                prev_period = period
                print(f"  -- {period} --")
            mark = ">>" if tag == "never" else "  "
            print(f"  {mark} {tee_dt.strftime('%-I:%M %p'):>9}   {LABELS[tag]}")
        per_day[play_date] = counts
        print(f"     {counts['never']} of {len(slots)} fringe slots went unsold")

    print("\n\n" + "=" * 62)
    print("TALLY")
    print("=" * 62)
    hdr = f"{'day':<14}{'unsold':>8}{'booked':>8}{'partial':>9}{'open':>7}"
    print(hdr)
    print("-" * len(hdr))
    for play_date in sorted(per_day):
        c = per_day[play_date]
        lab = datetime.strptime(play_date, "%m-%d-%Y").strftime("%a %b %d")
        print(f"{lab:<14}{c['never']:>8}{c['booked']:>8}"
              f"{c['partial']:>9}{c['open']:>7}")
    print("-" * len(hdr))
    print(f"{'TOTAL':<14}{totals['never']:>8}{totals['booked']:>8}"
          f"{totals['partial']:>9}{totals['open']:>7}")

    resolved = sum(totals[k] for k in ("never", "booked", "partial"))
    if resolved:
        pct = 100.0 * totals["never"] / resolved
        print(f"\n{totals['never']} of {resolved} resolved fringe slots "
              f"({pct:.0f}%) went completely unsold.")
    if totals["open"]:
        print(f"{totals['open']} slots haven't hit their cutoff yet.")


def probe():
    session = make_session()
    for bc in candidate_booking_classes(session):
        params = {
            "time": "all", "date": DATES[0], "holes": "all", "players": "0",
            "booking_class": bc, "schedule_id": SCHEDULE_ID,
            "schedule_ids[]": SCHEDULE_ID, "specials_only": "0",
            "api_key": "no_limits",
        }
        r = session.get(API_URL, params=params, timeout=25)
        print(f"\nbooking_class={bc!r} -> HTTP {r.status_code}, {len(r.text)} bytes")
        print("  " + r.text[:600].replace("\n", "\n  "))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snap"
    {"snap": snap, "report": report, "probe": probe}.get(cmd, snap)()
