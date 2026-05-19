#!/usr/bin/env python3
"""
Legion WFM → iCal Feed Generator  (RONA Enterprise Edition)
Portal: https://enterprise.legion.work/legion/?enterprise=rona

CONFIRMED working endpoint (found via browser fetch interceptor):
  GET /legion/schedule/getNextStaffingShifts?myShiftsOnly=true&size=<N>

NOTE: getScheduleWeekSummary is an admin/manager endpoint — returns 400 for
employee accounts regardless of auth. The employee schedule endpoint is
getNextStaffingShifts with myShiftsOnly=true.
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

LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
LEGION_ORG       = os.getenv("LEGION_ORG", "rona")
LEGION_TOKEN     = os.getenv("LEGION_TOKEN", "")
LEGION_DEVICE_ID = os.getenv("LEGION_DEVICE_ID", "")
WEEKS_AHEAD      = int(os.getenv("LEGION_WEEKS_AHEAD",  "8"))
WEEKS_BEHIND     = int(os.getenv("LEGION_WEEKS_BEHIND", "2"))
SHIFT_SIZE       = max(200, (WEEKS_AHEAD + WEEKS_BEHIND + 1) * 10)

OUTPUT_FILE = Path(os.getenv("ICAL_OUTPUT", "schedule.ics"))
LOCAL_TZ    = ZoneInfo("America/Toronto")
BASE_URL    = "https://enterprise.legion.work"
APP_URL     = f"{BASE_URL}/legion"
SHIFTS_ENDPOINT = f"{APP_URL}/schedule/getNextStaffingShifts"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s",
                    level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("legion-ical")


def date_range():
    today = datetime.now(LOCAL_TZ).date()
    return (today - timedelta(weeks=WEEKS_BEHIND)).isoformat(), \
           (today + timedelta(weeks=WEEKS_AHEAD)).isoformat()


def auth_variants(token):
    return [
        {"authToken": token},
        {"Authorization": f"Bearer {token}"},
        {"Authorization": f"Token {token}"},
        {"X-Auth-Token": token},
        {"X-Legion-Auth-Token": token},
        {},
    ]


def cookies():
    return {"DeviceID": LEGION_DEVICE_ID} if LEGION_DEVICE_ID else {}


def fetch_all_shifts(token):
    start_date, end_date = date_range()
    log.info("Fetching shifts %s → %s", start_date, end_date)
    log.info("Endpoint: %s", SHIFTS_ENDPOINT)

    base_headers = {
        "Accept":           "application/json",
        "Origin":           BASE_URL,
        "Referer":          f"{APP_URL}/?enterprise={LEGION_ORG}#/console/schedule/view",
        "X-Requested-With": "XMLHttpRequest",
    }

    def probe_urls():
        base = f"{SHIFTS_ENDPOINT}?myShiftsOnly=true&size={SHIFT_SIZE}"
        return [
            f"{base}&startDate={start_date}&endDate={end_date}",
            f"{base}&startOfWeek={start_date}",
            base,
        ]

    all_shifts = []
    working_url = working_auth = None
    logged_body = False

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for url in probe_urls():
            if working_auth is not None:
                break
            for auth in auth_variants(token):
                label = next(iter(auth)) if auth else "(cookie-only)"
                try:
                    resp = client.get(url, headers={**base_headers, **auth}, cookies=cookies())
                    if resp.status_code in (400, 401, 403) and not logged_body:
                        logged_body = True
                        log.info("First failure body: %s", resp.text[:300])
                    log.info("Probe [%s | %s]: HTTP %s",
                             url.split("?")[1][:60], label, resp.status_code)
                    if resp.status_code == 200:
                        working_url = url
                        working_auth = auth
                        log.info("✓ Success! auth=%s", label)
                        shifts = _extract_shifts(resp.json())
                        all_shifts.extend(shifts)
                        log.info("  → %d shift(s)", len(shifts))
                        break
                except Exception as exc:
                    log.debug("Probe error: %s", exc)

        if working_auth is None:
            raise RuntimeError(
                "All auth combinations failed.\n\n"
                "LEGION_TOKEN has likely expired (SAML SSO sessions are short-lived).\n\n"
                "To refresh:\n"
                "  1. Log into https://enterprise.legion.work/legion/?enterprise=rona\n"
                "  2. DevTools Console → type: localStorage.getItem('legion.authToken')\n"
                "  3. Copy the UUID and update the LEGION_TOKEN GitHub Secret."
            )

        if "startDate" not in working_url and WEEKS_BEHIND > 0:
            past_url = (f"{SHIFTS_ENDPOINT}?myShiftsOnly=true&size={SHIFT_SIZE}"
                        f"&startDate={start_date}&endDate={end_date}")
            try:
                resp = client.get(past_url, headers={**base_headers, **working_auth}, cookies=cookies())
                if resp.status_code == 200:
                    past = _extract_shifts(resp.json())
                    existing = {s.get("id") for s in all_shifts if s.get("id")}
                    all_shifts.extend(s for s in past if not s.get("id") or s["id"] not in existing)
                    log.info("Past-shifts → %d additional shift(s)", len(past))
            except Exception as exc:
                log.debug("Past-shifts error: %s", exc)

    log.info("Total raw shifts: %d", len(all_shifts))
    return all_shifts


def _extract_shifts(data):
    if data is None:
        return []
    if isinstance(data, list):
        flat = []
        for item in data:
            if _looks_like_shift(item):
                flat.append(item)
            elif isinstance(item, dict):
                for key in ("shifts", "scheduledShifts", "assignedShifts",
                            "staffingShifts", "myShifts", "nextShifts"):
                    flat.extend(s for s in (item.get(key) or []) if _looks_like_shift(s))
        return flat
    if isinstance(data, dict):
        for key in ("shifts", "scheduledShifts", "assignedShifts", "staffingShifts",
                    "myShifts", "nextShifts", "schedule", "schedules",
                    "data", "items", "results"):
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


def _looks_like_shift(obj):
    if not isinstance(obj, dict):
        return False
    return any(k in obj for k in (
        "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
        "startDateTime", "shiftStartTime",
    ))


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


def parse_time(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = raw / 1000 if raw > 1e11 else raw
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw.strip().rstrip("Z"), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _get(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _name(f):
    if isinstance(f, dict):
        return f.get("name", "") or f.get("displayName", "")
    return str(f) if f else ""


def ical_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def shift_to_vevent(shift):
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
    uid = hashlib.sha256(
        f"{start.isoformat()}|{end.isoformat()}|{shift.get('id','')}".encode()
    ).hexdigest()[:32] + "@legion-rona"
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
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID={tzid}:{sl.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tzid}:{el.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{ical_escape(summary)}", f"DESCRIPTION:{desc}",
    ]
    if location:
        lines.append(f"LOCATION:{ical_escape(location)}")
    lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT"]
    return "\n".join(lines)


def build_ical(shifts):
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
    log.info("Unique events: %d", len(vevents))
    return ICAL_HEADER + "\n".join(vevents) + "\n" + ICAL_FOOTER


def main():
    dry_run = "--dry-run" in sys.argv
    log.info("RONA Legion iCal Scraper starting …")
    if not LEGION_TOKEN:
        sys.exit("ERROR: LEGION_TOKEN is not set. Add it as a GitHub Secret.")
    log.info("Using LEGION_TOKEN (SAML SSO session token)")
    shifts = fetch_all_shifts(LEGION_TOKEN)
    ical   = build_ical(shifts)
    if dry_run:
        print(ical)
        return
    OUTPUT_FILE.write_text(ical, encoding="utf-8")
    log.info("✓ Written: %s (%d bytes)", OUTPUT_FILE, OUTPUT_FILE.stat().st_size)


if __name__ == "__main__":
    main()
