#!/usr/bin/env python3
"""
Legion WFM → iCal Feed Generator  (RONA Enterprise Edition)
Portal: https://enterprise.legion.work/legion/?enterprise=rona

Confirmed API endpoint:
  GET /legion/schedule/getScheduleWeekSummary?startOfWeek=<ISO>

Authentication: LEGION_TOKEN (UUID from localStorage 'legion.authToken')
                LEGION_DEVICE_ID (DeviceID cookie value)
"""

import hashlib
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO").upper()
LEGION_ORG          = os.getenv("LEGION_ORG", "rona")
LEGION_TOKEN        = os.getenv("LEGION_TOKEN", "")
LEGION_DEVICE_ID    = os.getenv("LEGION_DEVICE_ID", "")
LEGION_API_ENDPOINT = os.getenv("LEGION_API_ENDPOINT", "")
WEEKS_AHEAD         = int(os.getenv("LEGION_WEEKS_AHEAD",  "8"))
WEEKS_BEHIND        = int(os.getenv("LEGION_WEEKS_BEHIND", "2"))

OUTPUT_FILE = Path(os.getenv("ICAL_OUTPUT", "schedule.ics"))
LOCAL_TZ    = ZoneInfo("America/Toronto")

BASE_URL = "https://enterprise.legion.work"
APP_URL  = f"{BASE_URL}/legion"
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


# ──────────────────────────────────────────────────────────────────────
# Week calculation
# ──────────────────────────────────────────────────────────────────────
def week_starts(weeks_behind: int, weeks_ahead: int) -> list[str]:
    today = datetime.now(LOCAL_TZ).date()
    days_until_saturday = (5 - today.weekday()) % 7
    base = today + timedelta(days=days_until_saturday)   # next (or current) Saturday

    starts = []
    for offset in range(-weeks_behind, weeks_ahead + 1):
        d = base + timedelta(weeks=offset)
        starts.append(f"{d.isoformat()}T04:00:00.000Z")
    return starts


# ──────────────────────────────────────────────────────────────────────
# Auth helpers
# ──────────────────────────────────────────────────────────────────────
def auth_variants(token: str) -> list[dict]:
    return [
        {"authToken": token},
        {"Authorization": f"Bearer {token}"},
        {"Authorization": f"Token {token}"},
        {"X-Auth-Token": token},
        {"X-Legion-Auth-Token": token},
        {"legion-auth-token": token},
        {},                           # cookie-only probe
    ]


def cookies() -> dict:
    c = {}
    if LEGION_DEVICE_ID:
        c["DeviceID"] = LEGION_DEVICE_ID
    return c


# ──────────────────────────────────────────────────────────────────────
# Schedule fetching
# ──────────────────────────────────────────────────────────────────────
def fetch_all_shifts(token: str) -> list[dict]:
    weeks = week_starts(WEEKS_BEHIND, WEEKS_AHEAD)
    probe_week = weeks[WEEKS_BEHIND]   # offset 0 = next/current Saturday

    log.info("Fetching %d week(s), probe week: %s", len(weeks), probe_week)
    log.info("Endpoint: %s", SCHEDULE_ENDPOINT)

    # NOTE: No Content-Type on GET requests — body-less GET + Content-Type
    # can trigger HTTP 400 on many servers / WAFs.
    get_headers = {
        "Accept":           "application/json",
        "Origin":           BASE_URL,
        "Referer":          f"{APP_URL}/?enterprise={LEGION_ORG}#/console/schedule/view",
        "X-Requested-With": "XMLHttpRequest",
    }
    post_headers = {**get_headers, "Content-Type": "application/json"}

    all_shifts: list[dict] = []
    working_auth:   dict | None = None
    working_method: str | None = None

    with httpx.Client(timeout=30, follow_redirects=True) as client:

        # ── Discovery probe: try GET then POST × every auth format ──
        for method in ("GET", "POST"):
            if working_auth is not None:
                break
            for auth in auth_variants(token):
                try:
                    base  = get_headers if method == "GET" else post_headers
                    hdrs  = {**base, **auth}
                    label = next(iter(auth)) if auth else "(cookie-only)"

                    if method == "GET":
                        resp = client.get(
                            f"{SCHEDULE_ENDPOINT}?startOfWeek={probe_week}",
                            headers=hdrs, cookies=cookies(),
                        )
                    else:
                        resp = client.post(
                            SCHEDULE_ENDPOINT,
                            json={"startOfWeek": probe_week},
                            headers=hdrs, cookies=cookies(),
                        )

                    log.info("Probe [%s %s]: HTTP %s", method, label, resp.status_code)

                    # ★ Log the 400 body — this tells us exactly what the server wants
                    if resp.status_code == 400 and not getattr(fetch_all_shifts, "_logged_400", False):
                        fetch_all_shifts._logged_400 = True
                        try:
                            log.info("400 response body: %s", resp.text[:800])
                        except Exception:
                            pass

                    if resp.status_code == 200:
                        working_auth   = auth
                        working_method = method
                        log.info("✓ Auth confirmed: %s %s", method, label)
                        shifts = _extract_shifts(resp.json())
                        all_shifts.extend(shifts)
                        log.info("  Week %s → %d shift(s)", probe_week[:10], len(shifts))
                        break

                except Exception as exc:
                    log.debug("Probe error: %s", exc)

        if working_auth is None:
            raise RuntimeError(
                "All auth formats failed on both GET and POST.\n\n"
                "IMPORTANT: Check the '400 response body' line above in the logs.\n"
                "That message tells us exactly what the server is rejecting.\n\n"
                "If no body was logged, copy ALL Request Headers from DevTools:\n"
                "  1. Log into https://enterprise.legion.work/legion/?enterprise=rona\n"
                "  2. Right-click → Inspect → Network tab → Fetch/XHR filter\n"
                "  3. Navigate to your schedule\n"
                "  4. Click the getScheduleWeekSummary request\n"
                "  5. Copy ALL Request Headers and paste them here.\n\n"
                "Also verify LEGION_TOKEN is current:\n"
                "  Application → Local Storage → enterprise.legion.work → legion.authToken"
            )

        # ── Fetch remaining weeks with confirmed auth ────────────────
        fetched        = {probe_week}
        base_for_fetch = get_headers if working_method == "GET" else post_headers
        hdrs           = {**base_for_fetch, **working_auth}

        for week in weeks:
            if week in fetched:
                continue
            fetched.add(week)
            try:
                if working_method == "GET":
                    resp = client.get(
                        f"{SCHEDULE_ENDPOINT}?startOfWeek={week}",
                        headers=hdrs, cookies=cookies(),
                    )
                else:
                    resp = client.post(
                        SCHEDULE_ENDPOINT,
                        json={"startOfWeek": week},
                        headers=hdrs, cookies=cookies(),
                    )

                if resp.status_code == 200:
                    shifts = _extract_shifts(resp.json())
                    all_shifts.extend(shifts)
                    log.info("  Week %s → %d shift(s)", week[:10], len(shifts))
                elif resp.status_code == 401:
                    log.warning("401 on week %s — token expired?", week[:10])
                    break
                else:
                    log.debug("HTTP %s for week %s", resp.status_code, week[:10])
            except Exception as exc:
                log.warning("Error fetching week %s: %s", week[:10], exc)

    log.info("Total raw shifts: %d", len(all_shifts))
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
                    flat.extend(s for s in (item.get(key) or []) if _looks_like_shift(s))
        return flat
    if isinstance(data, dict):
        for key in ("shifts", "scheduledShifts", "assignedShifts", "schedule",
                    "schedules", "data", "items", "results", "weekSummary"):
            val = data.get(key)
            if isinstance(val, list):
                r = _extract_shifts(val)
                if r:
                    return r
            elif isinstance(val, dict):
                r = _extract_shifts(val)
                if r:
                    return r
        if _looks_like_shift(data):
            return [data]
        for val in data.values():
            if isinstance(val, list) and val:
                r = _extract_shifts(val)
                if r:
                    return r
    return []


def _looks_like_shift(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    return any(k in obj for k in (
        "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
        "startDateTime", "shiftStartTime",
    ))


# ──────────────────────────────────────────────────────────────────────
# iCal generation
# ──────────────────────────────────────────────────────────────────────
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
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw.strip().rstrip("Z"), fmt).replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _get(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _name(f) -> str:
    if isinstance(f, dict):
        return f.get("name", "") or f.get("displayName", "")
    return str(f) if f else ""


def ical_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def shift_to_vevent(shift: dict) -> str | None:
    start = parse_time(_get(shift, "startTime", "start", "startDate", "shiftStart",
                            "scheduledStart", "startTimestamp", "startAt",
                            "startDateTime", "shiftStartTime"))
    end   = parse_time(_get(shift, "endTime", "end", "endDate", "shiftEnd",
                            "scheduledEnd", "endTimestamp", "endAt",
                            "endDateTime", "shiftEndTime"))
    if not start or not end or end <= start:
        return None

    sl  = start.astimezone(LOCAL_TZ)
    el  = end.astimezone(LOCAL_TZ)
    uid = (hashlib.sha256(
        f"{start.isoformat()}|{end.isoformat()}|{shift.get('id','')}".encode()
    ).hexdigest()[:32] + "@legion-rona")

    location = _name(_get(shift, "location", "locationName", "storeName"))
    role     = _name(_get(shift, "role", "roleName", "position", "positionName", "jobTitle"))
    dept     = _name(_get(shift, "department", "departmentName", "area", "areaName"))
    summary  = " – ".join(filter(None, ["RONA Shift", role or dept]))
    dur_h    = (end - start).total_seconds() / 3600

    desc = r"\n".join(filter(None, [
        f"Duration: {dur_h:.1f}h",
        f"Location: {location}" if location else "",
        f"Role: {role}"         if role     else "",
        f"Dept: {dept}"         if dept     else "",
        (f"Notes: {_get(shift,'notes','note','comment')}"
         if _get(shift, "notes", "note", "comment") else ""),
    ]))

    tzid    = "America/Toronto"
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines   = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID={tzid}:{sl.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tzid}:{el.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{ical_escape(summary)}",
        f"DESCRIPTION:{desc}",
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
        m   = re.search(r"UID:(.+)", vevent)
        uid = m.group(1) if m else str(uuid.uuid4())
        if uid not in seen:
            seen.add(uid)
            vevents.append(vevent)
    log.info("Unique events to write: %d", len(vevents))
    return ICAL_HEADER + "\n".join(vevents) + "\n" + ICAL_FOOTER


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────
def main() -> None:
    dry_run = "--dry-run" in sys.argv
    log.info("RONA Legion iCal Scraper starting …")

    if not LEGION_TOKEN:
        sys.exit(
            "ERROR: LEGION_TOKEN is not set.\n"
            "Add it as a GitHub Secret → Settings → Secrets → Actions.\n"
            "Value: UUID from localStorage key 'legion.authToken'"
        )

    log.info("Using LEGION_TOKEN (UUID)")
    shifts = fetch_all_shifts(LEGION_TOKEN)
    ical   = build_ical(shifts)

    if dry_run:
        print(ical)
        return

    OUTPUT_FILE.write_text(ical, encoding="utf-8")
    log.info("✓ Written: %s (%d bytes)", OUTPUT_FILE, OUTPUT_FILE.stat().st_size)


if __name__ == "__main__":
    main()
