#!/usr/bin/env python3
"""
Legion WFM → iCal Feed Generator  (RONA Enterprise Edition)
Portal: https://enterprise.legion.work/legion/?enterprise=rona

Now uses the confirmed API endpoint:
  GET /legion/schedule/getScheduleWeekSummary?startOfWeek=<ISO>

Authentication: LEGION_TOKEN secret (UUID token from localStorage key
"legion.authToken").  No browser login required.
"""

import hashlib
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO").upper()
LEGION_ORG          = os.getenv("LEGION_ORG", "rona")
LEGION_TOKEN        = os.getenv("LEGION_TOKEN", "")
LEGION_API_ENDPOINT = os.getenv("LEGION_API_ENDPOINT", "")   # optional override
WEEKS_AHEAD         = int(os.getenv("LEGION_WEEKS_AHEAD",  "8"))
WEEKS_BEHIND        = int(os.getenv("LEGION_WEEKS_BEHIND", "2"))

OUTPUT_FILE = Path(os.getenv("ICAL_OUTPUT", "schedule.ics"))
LOCAL_TZ    = ZoneInfo("America/Toronto")

BASE_URL = "https://enterprise.legion.work"
APP_URL  = f"{BASE_URL}/legion"

# ── Confirmed endpoint (discovered via DevTools Network tab) ──────────
SCHEDULE_ENDPOINT = (
    LEGION_API_ENDPOINT.split("?")[0]   # strip any existing query params
    if LEGION_API_ENDPOINT
    else f"{APP_URL}/schedule/getScheduleWeekSummary"
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger("legion-ical")


# ──────────────────────────────────────────────
# Week calculation
# ──────────────────────────────────────────────
def week_starts(weeks_behind: int, weeks_ahead: int) -> list[str]:
    """
    Return a list of ISO-8601 startOfWeek timestamps covering the requested
    range.  RONA's Legion install uses Saturday as the start of the scheduling
    week, expressed as midnight Eastern time = 04:00:00.000Z (UTC, summer).

    e.g. 2026-05-23T04:00:00.000Z  →  Saturday 23 May 2026 00:00 EDT
    """
    today = datetime.now(LOCAL_TZ).date()

    # Day-of-week: Mon=0 … Sat=5, Sun=6
    # Days since last Saturday:
    days_since_saturday = (today.weekday() - 5) % 7
    current_week_start  = today - timedelta(days=days_since_saturday)

    starts = []
    for offset in range(-weeks_behind, weeks_ahead + 1):
        week_date = current_week_start + timedelta(weeks=offset)
        # Express as UTC timestamp: midnight ET = 04:00 UTC during EDT (UTC-4)
        # Use a fixed 04:00 offset which matches the observed API value.
        iso = f"{week_date.isoformat()}T04:00:00.000Z"
        starts.append(iso)

    return starts


# ──────────────────────────────────────────────
# Auth headers — try multiple strategies
# ──────────────────────────────────────────────
def auth_header_variants(token: str) -> list[dict]:
    """Return candidate auth header dicts to try in order."""
    return [
        {"Authorization": f"Bearer {token}"},
        {"Authorization": f"Token {token}"},
        {"X-Auth-Token": token},
        {"X-Legion-Auth-Token": token},
        # Some enterprise installs use a session cookie instead
        # (httpx doesn't auto-set cookies; handled separately)
    ]


# ──────────────────────────────────────────────
# Schedule fetching
# ──────────────────────────────────────────────
def fetch_all_shifts(token: str) -> list[dict]:
    """
    Call getScheduleWeekSummary once per week for the configured date range.
    Returns a flat list of all shift dicts found.
    """
    weeks = week_starts(WEEKS_BEHIND, WEEKS_AHEAD)
    log.info(
        "Fetching %d week(s) from %s → %s via %s",
        len(weeks), weeks[0][:10], weeks[-1][:10], SCHEDULE_ENDPOINT,
    )

    common_headers = {
        "Accept":           "application/json",
        "Content-Type":     "application/json",
        "Origin":           BASE_URL,
        "Referer":          f"{APP_URL}/?enterprise={LEGION_ORG}",
        "X-Requested-With": "XMLHttpRequest",
    }

    all_shifts: list[dict] = []
    working_auth: dict | None = None   # cache the first successful auth header

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # ── Discover which auth header format works ───────────────
        if working_auth is None:
            probe_week = weeks[min(WEEKS_BEHIND, len(weeks) - 1)]  # current week
            for auth in auth_header_variants(token):
                try:
                    headers = {**common_headers, **auth}
                    # Build URL manually — httpx encodes colons in params
                    # (%3A) which this server rejects with HTTP 400.
                    resp = client.get(
                        f"{SCHEDULE_ENDPOINT}?startOfWeek={probe_week}",
                        headers=headers,
                    )
                    log.info(
                        "Auth probe [%s]: HTTP %s",
                        list(auth.keys())[0], resp.status_code,
                    )
                    if resp.status_code == 200:
                        working_auth = auth
                        log.info(
                            "Auth header confirmed: %s", list(auth.keys())[0]
                        )
                        # Parse this first response too
                        shifts = _extract_shifts(resp.json())
                        if shifts:
                            all_shifts.extend(shifts)
                            log.info(
                                "  Week %s → %d shift(s)", probe_week[:10], len(shifts)
                            )
                        break
                    elif resp.status_code == 401:
                        log.debug("401 with %s", list(auth.keys())[0])
                    elif resp.status_code == 403:
                        log.debug("403 with %s", list(auth.keys())[0])
                except Exception as exc:
                    log.debug("Probe error: %s", exc)

            if working_auth is None:
                raise RuntimeError(
                    "All auth header formats failed (400/401/403).\n\n"
                    "Your LEGION_TOKEN may have expired.  To refresh it:\n"
                    "1. Log into https://enterprise.legion.work/legion/?enterprise=rona\n"
                    "2. Open DevTools (F12 or right-click → Inspect)\n"
                    "3. Application → Local Storage → enterprise.legion.work\n"
                    "4. Copy the value of 'legion.authToken'\n"
                    "5. Update the LEGION_TOKEN GitHub Secret with the new value."
                )

        # ── Fetch remaining weeks ─────────────────────────────────
        fetched_weeks = {weeks[min(WEEKS_BEHIND, len(weeks) - 1)]}
        headers = {**common_headers, **working_auth}

        for week in weeks:
            if week in fetched_weeks:
                continue
            fetched_weeks.add(week)
            try:
                resp = client.get(
                    f"{SCHEDULE_ENDPOINT}?startOfWeek={week}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    shifts = _extract_shifts(resp.json())
                    if shifts:
                        all_shifts.extend(shifts)
                    log.info(
                        "  Week %s → %d shift(s)", week[:10], len(shifts or [])
                    )
                elif resp.status_code == 401:
                    log.warning("401 on week %s — token may have expired", week[:10])
                    break
                else:
                    log.debug("HTTP %s for week %s", resp.status_code, week[:10])
            except Exception as exc:
                log.warning("Error fetching week %s: %s", week[:10], exc)

    log.info("Total raw shifts collected: %d", len(all_shifts))
    return all_shifts


def _extract_shifts(data: object) -> list[dict]:
    """
    Normalise the getScheduleWeekSummary response into a flat list of shifts.
    Handles multiple possible response shapes.
    """
    if data is None:
        return []

    # Direct list
    if isinstance(data, list):
        flat = []
        for item in data:
            if _looks_like_shift(item):
                flat.append(item)
            elif isinstance(item, dict):
                # Might be a day-grouped entry
                for key in ("shifts", "scheduledShifts", "assignedShifts"):
                    sub = item.get(key)
                    if isinstance(sub, list):
                        flat.extend(s for s in sub if _looks_like_shift(s))
        return flat

    if isinstance(data, dict):
        # Try common top-level keys
        for key in (
            "shifts", "scheduledShifts", "assignedShifts",
            "schedule", "schedules", "data", "items", "results",
            "weekSummary", "summary",
        ):
            val = data.get(key)
            if isinstance(val, list):
                extracted = _extract_shifts(val)
                if extracted:
                    return extracted
            elif isinstance(val, dict):
                extracted = _extract_shifts(val)
                if extracted:
                    return extracted

        # Maybe the dict IS a single shift
        if _looks_like_shift(data):
            return [data]

        # Recurse into any list values
        for val in data.values():
            if isinstance(val, list) and val:
                extracted = _extract_shifts(val)
                if extracted:
                    return extracted

    return []


def _looks_like_shift(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    return any(k in obj for k in (
        "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
        "startDateTime", "shiftStartTime",
    ))


# ──────────────────────────────────────────────
# iCal generation
# ──────────────────────────────────────────────
ICAL_HEADER = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//legion-ical//RONA Schedule//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:RONA Work Schedule
X-WR-TIMEZONE:America/Toronto
X-WR-CALDESC:Auto-generated from Legion WFM
REFRESH-INTERVAL;VALUE=DURATION:PT4H
X-PUBLISHED-TTL:PT4H
"""
ICAL_FOOTER = "END:VCALENDAR\n"


def parse_time(raw) -> datetime | None:
    """Parse epoch millis, epoch seconds, or ISO-8601 string into UTC datetime."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = raw / 1000 if raw > 1e11 else raw
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        s = raw.strip().rstrip("Z")
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",       "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _get(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _name(field) -> str:
    if isinstance(field, dict):
        return field.get("name", "") or field.get("displayName", "")
    return str(field) if field else ""


def ical_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def shift_to_vevent(shift: dict) -> str | None:
    start = parse_time(_get(shift,
        "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
        "startDateTime", "shiftStartTime",
    ))
    end = parse_time(_get(shift,
        "endTime", "end", "endDate", "shiftEnd",
        "scheduledEnd", "endTimestamp", "endAt",
        "endDateTime", "shiftEndTime",
    ))
    if not start or not end:
        log.debug("Skipping shift — no parseable times: %s", list(shift.keys()))
        return None

    # If start == end or end before start, skip
    if end <= start:
        log.debug("Skipping shift — end <= start")
        return None

    sl = start.astimezone(LOCAL_TZ)
    el = end.astimezone(LOCAL_TZ)

    # Stable UID derived from shift content — guarantees no duplicates on re-run
    uid_seed = f"{start.isoformat()}|{end.isoformat()}|{shift.get('id', '')}"
    uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:32] + "@legion-rona"

    location = _name(_get(shift, "location", "locationName", "storeName"))
    role     = _name(_get(shift, "role", "roleName", "position", "positionName", "jobTitle"))
    dept     = _name(_get(shift, "department", "departmentName", "area", "areaName"))

    summary  = " – ".join(filter(None, ["RONA Shift", role or dept]))
    dur_h    = (end - start).total_seconds() / 3600

    desc_lines = [f"Duration: {dur_h:.1f}h"]
    if location: desc_lines.append(f"Location: {location}")
    if role:     desc_lines.append(f"Role: {role}")
    if dept:     desc_lines.append(f"Dept: {dept}")
    note = _get(shift, "notes", "note", "comment", "description")
    if note:     desc_lines.append(f"Notes: {note}")
    description = r"\n".join(desc_lines)

    tzid    = "America/Toronto"
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID={tzid}:{sl.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tzid}:{el.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{ical_escape(summary)}",
        f"DESCRIPTION:{description}",
    ]
    if location:
        lines.append(f"LOCATION:{ical_escape(location)}")
    lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT"]
    return "\n".join(lines)


def build_ical(shifts: list[dict]) -> str:
    vevents, seen = [], set()
    for shift in shifts:
        vevent = shift_to_vevent(shift)
        if not vevent:
            continue
        m = re.search(r"UID:(.+)", vevent)
        uid = m.group(1) if m else str(uuid.uuid4())
        if uid not in seen:
            seen.add(uid)
            vevents.append(vevent)

    log.info("Unique events to write: %d", len(vevents))
    return ICAL_HEADER + "\n".join(vevents) + "\n" + ICAL_FOOTER


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    dry_run = "--dry-run" in sys.argv

    log.info("RONA Legion iCal Scraper starting …")
    log.info("Endpoint: %s", SCHEDULE_ENDPOINT)

    if not LEGION_TOKEN:
        sys.exit(
            "ERROR: LEGION_TOKEN is not set.\n"
            "Add it as a GitHub Secret (Settings → Secrets → Actions).\n"
            "Value: the UUID from localStorage key 'legion.authToken'"
        )

    log.info("Using LEGION_TOKEN (UUID auth token)")

    shifts       = fetch_all_shifts(LEGION_TOKEN)
    ical_content = build_ical(shifts)

    if dry_run:
        print(ical_content)
        return

    OUTPUT_FILE.write_text(ical_content, encoding="utf-8")
    log.info("✓ Written: %s (%d bytes)", OUTPUT_FILE, OUTPUT_FILE.stat().st_size)


if __name__ == "__main__":
    main()
