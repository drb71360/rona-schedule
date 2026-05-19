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
LEGION_DEVICE_ID    = os.getenv("LEGION_DEVICE_ID", "")      # DeviceID cookie value
LEGION_API_ENDPOINT = os.getenv("LEGION_API_ENDPOINT", "")   # optional override
WEEKS_AHEAD         = int(os.getenv("LEGION_WEEKS_AHEAD",  "8"))
WEEKS_BEHIND        = int(os.getenv("LEGION_WEEKS_BEHIND", "2"))

OUTPUT_FILE = Path(os.getenv("ICAL_OUTPUT", "schedule.ics"))
LOCAL_TZ    = ZoneInfo("America/Toronto")

BASE_URL = "https://enterprise.legion.work"
APP_URL  = f"{BASE_URL}/legion"

# ── Confirmed endpoint (discovered via DevTools Network tab) ──────────
SCHEDULE_ENDPOINT = (
    LEGION_API_ENDPOINT.split("?")[0]
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
    Return ISO-8601 startOfWeek timestamps.
    RONA uses Saturday as week start = midnight Eastern = T04:00:00.000Z (EDT).
    """
    today = datetime.now(LOCAL_TZ).date()
    days_since_saturday = (today.weekday() - 5) % 7
    current_week_start  = today - timedelta(days=days_since_saturday)

    starts = []
    for offset in range(-weeks_behind, weeks_ahead + 1):
        week_date = current_week_start + timedelta(weeks=offset)
        iso = f"{week_date.isoformat()}T04:00:00.000Z"
        starts.append(iso)
    return starts


# ──────────────────────────────────────────────
# Auth headers — try multiple strategies
# ──────────────────────────────────────────────
def auth_header_variants(token: str) -> list[dict]:
    """
    Try every plausible auth header format.
    'authToken' is the most likely — it mirrors the localStorage key name
    the SPA uses ('legion.authToken') minus the namespace prefix.
    """
    return [
        {"authToken": token},                    # most likely — matches LS key
        {"Authorization": f"Bearer {token}"},
        {"Authorization": f"Token {token}"},
        {"X-Auth-Token": token},
        {"X-Legion-Auth-Token": token},
        {"legion-auth-token": token},
        {},                                       # cookie-only (no auth header)
    ]


def session_cookies() -> dict:
    """Always send the DeviceID cookie — the server may require it."""
    cookies = {}
    if LEGION_DEVICE_ID:
        cookies["DeviceID"] = LEGION_DEVICE_ID
    return cookies


# ──────────────────────────────────────────────
# Schedule fetching
# ──────────────────────────────────────────────
def fetch_all_shifts(token: str) -> list[dict]:
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
    working_auth: dict | None = None

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # ── Probe to find working auth format ───────────────────
        probe_week = weeks[min(WEEKS_BEHIND, len(weeks) - 1)]
        cookies    = session_cookies()

        for auth in auth_header_variants(token):
            try:
                headers = {**common_headers, **auth}
                resp = client.get(
                    f"{SCHEDULE_ENDPOINT}?startOfWeek={probe_week}",
                    headers=headers,
                    cookies=cookies,
                )
                label = list(auth.keys())[0] if auth else "(no auth header)"
                log.info("Auth probe [%s]: HTTP %s", label, resp.status_code)

                if resp.status_code == 200:
                    working_auth = auth
                    log.info("✓ Auth confirmed: %s", label)
                    shifts = _extract_shifts(resp.json())
                    if shifts:
                        all_shifts.extend(shifts)
                        log.info("  Week %s → %d shift(s)", probe_week[:10], len(shifts))
                    break

            except Exception as exc:
                log.debug("Probe error: %s", exc)

        if working_auth is None:
            raise RuntimeError(
                "All auth formats failed.\n\n"
                "Most likely cause: LEGION_TOKEN has expired.\n\n"
                "To get a fresh token:\n"
                "  1. Log into https://enterprise.legion.work/legion/?enterprise=rona\n"
                "  2. Right-click anywhere → Inspect → Application tab\n"
                "  3. Local Storage → enterprise.legion.work\n"
                "  4. Copy the value of 'legion.authToken'\n"
                "  5. Update the LEGION_TOKEN secret in GitHub:\n"
                "     Settings → Secrets and variables → Actions"
            )

        # ── Fetch remaining weeks ────────────────────────────────
        fetched = {probe_week}
        headers = {**common_headers, **working_auth}

        for week in weeks:
            if week in fetched:
                continue
            fetched.add(week)
            try:
                resp = client.get(
                    f"{SCHEDULE_ENDPOINT}?startOfWeek={week}",
                    headers=headers,
                    cookies=session_cookies(),
                )
                if resp.status_code == 200:
                    shifts = _extract_shifts(resp.json())
                    all_shifts.extend(shifts or [])
                    log.info("  Week %s → %d shift(s)", week[:10], len(shifts or []))
                elif resp.status_code == 401:
                    log.warning("401 on week %s — token expired?", week[:10])
                    break
                else:
                    log.debug("HTTP %s for week %s", resp.status_code, week[:10])
            except Exception as exc:
                log.warning("Error fetching week %s: %s", week[:10], exc)

    log.info("Total raw shifts collected: %d", len(all_shifts))
    return all_shifts


def _extract_shifts(data: object) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        flat = []
        for item in data:
            if _looks_like_shift(item):
                flat.append(item)
            elif isinstance(item, dict):
                for key in ("shifts", "scheduledShifts", "assignedShifts"):
                    sub = item.get(key)
                    if isinstance(sub, list):
                        flat.extend(s for s in sub if _looks_like_shift(s))
        return flat
    if isinstance(data, dict):
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
        if _looks_like_shift(data):
            return [data]
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
    if not start or not end or end <= start:
        return None

    sl = start.astimezone(LOCAL_TZ)
    el = end.astimezone(LOCAL_TZ)

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
