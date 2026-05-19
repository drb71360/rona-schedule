#!/usr/bin/env python3
"""
Legion WFM → iCal Feed Generator
Logs into the Legion employee portal, fetches your shift schedule,
and writes a standards-compliant .ics file ready for Google Calendar
/ Thunderbird / Apple Calendar subscription.

Usage
-----
  python scraper.py              # full run: login → fetch → write schedule.ics
  python scraper.py --fetch-only # reuse cached token, skip browser login
  python scraper.py --dry-run    # print shifts to stdout, don't write file

Environment variables (set in shell or GitHub Secrets)
-------------------------------------------------------
  LEGION_EMAIL      your Legion login e-mail
  LEGION_PASSWORD   your Legion password
  LEGION_ORG        org slug shown in the URL after login, e.g. "rona"
                    (leave blank if unsure – the script will auto-detect)
  LEGION_WEEKS_AHEAD   how many weeks forward to fetch (default 8)
  LEGION_WEEKS_BEHIND  how many weeks back to fetch  (default 2)
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from playwright.async_api import async_playwright

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
LEGION_EMAIL    = os.getenv("LEGION_EMAIL", "")
LEGION_PASSWORD = os.getenv("LEGION_PASSWORD", "")
LEGION_ORG      = os.getenv("LEGION_ORG", "")          # e.g. "rona"
WEEKS_AHEAD     = int(os.getenv("LEGION_WEEKS_AHEAD",  "8"))
WEEKS_BEHIND    = int(os.getenv("LEGION_WEEKS_BEHIND", "2"))

TOKEN_CACHE     = Path(".legion_token.json")
OUTPUT_FILE     = Path(os.getenv("ICAL_OUTPUT", "schedule.ics"))
LOCAL_TZ        = ZoneInfo("America/Toronto")

# Legion uses these base URLs
BASE_URL        = "https://app.legion.co"
API_BASE        = f"{BASE_URL}/api"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger("legion-ical")


# ──────────────────────────────────────────────
# Token cache helpers
# ──────────────────────────────────────────────
def load_cached_token() -> dict | None:
    """Return cached auth token if it is still valid (>5 min left)."""
    if not TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE.read_text())
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at - datetime.now(timezone.utc) > timedelta(minutes=5):
            log.debug("Using cached auth token (expires %s)", expires_at)
            return data
        log.debug("Cached token expired")
    except Exception as exc:
        log.debug("Token cache unreadable: %s", exc)
    return None


def save_token(token: str, employee_id: str, org_id: str, ttl_hours: int = 8) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    TOKEN_CACHE.write_text(json.dumps({
        "token":       token,
        "employee_id": employee_id,
        "org_id":      org_id,
        "expires_at":  expires_at.isoformat(),
    }))
    TOKEN_CACHE.chmod(0o600)
    log.debug("Token cached until %s", expires_at)


# ──────────────────────────────────────────────
# Direct API login (fast path – no browser)
# ──────────────────────────────────────────────
def try_direct_login(email: str, password: str) -> dict | None:
    """
    Attempt a direct REST login.  Returns token info dict on success or None.
    Legion's login endpoint varies by org configuration; we try the most
    common patterns.
    """
    endpoints = [
        f"{API_BASE}/v1/authentication/login",
        f"{API_BASE}/v2/authentication/login",
        f"{API_BASE}/v1/auth/login",
    ]
    payload = {"email": email, "password": password}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for url in endpoints:
            try:
                log.debug("Trying direct login at %s", url)
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    token = (
                        data.get("token")
                        or data.get("access_token")
                        or data.get("authToken")
                        or data.get("jwt")
                    )
                    if token:
                        # employee / org IDs may be nested
                        employee_id = str(
                            data.get("employeeId")
                            or data.get("employee", {}).get("id", "")
                        )
                        org_id = str(
                            data.get("orgId")
                            or data.get("org", {}).get("id", "")
                        )
                        log.info("Direct login succeeded (%s)", url)
                        return {"token": token, "employee_id": employee_id, "org_id": org_id}
                log.debug("Direct login %s returned %s", url, resp.status_code)
            except Exception as exc:
                log.debug("Direct login error at %s: %s", url, exc)
    return None


# ──────────────────────────────────────────────
# Playwright browser login (fallback / SSO)
# ──────────────────────────────────────────────
async def browser_login(email: str, password: str, org_slug: str) -> dict:
    """
    Open the Legion login page in a headless browser, sign in, and
    capture the bearer token by intercepting XHR/fetch responses.
    Returns token info dict.
    """
    log.info("Starting browser-based login …")
    captured: dict = {}

    login_url = (
        f"{BASE_URL}/{org_slug}/login" if org_slug
        else f"{BASE_URL}/login"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # Intercept API responses to grab the auth token
        async def on_response(response):
            if captured:
                return
            url = response.url
            if "login" in url or "auth" in url or "authentication" in url:
                try:
                    if response.status in (200, 201):
                        body = await response.json()
                        token = (
                            body.get("token")
                            or body.get("access_token")
                            or body.get("authToken")
                            or body.get("jwt")
                        )
                        if token:
                            captured["token"]       = token
                            captured["employee_id"] = str(body.get("employeeId") or "")
                            captured["org_id"]      = str(body.get("orgId") or "")
                            log.debug("Token captured from network intercept")
                except Exception:
                    pass

        page.on("response", on_response)

        # Also listen for localStorage after navigation
        log.info("Navigating to %s", login_url)
        await page.goto(login_url, wait_until="networkidle", timeout=30_000)

        # Fill credentials
        await _fill_login_form(page, email, password)

        # Wait for navigation or token capture
        for _ in range(30):
            await asyncio.sleep(1)
            if captured:
                break
            # Check localStorage as fallback
            try:
                ls_token = await page.evaluate(
                    "() => localStorage.getItem('token') "
                    "|| localStorage.getItem('authToken') "
                    "|| localStorage.getItem('jwt')"
                )
                if ls_token:
                    captured["token"] = ls_token
                    captured["employee_id"] = await page.evaluate(
                        "() => localStorage.getItem('employeeId') || ''"
                    ) or ""
                    captured["org_id"] = await page.evaluate(
                        "() => localStorage.getItem('orgId') || ''"
                    ) or ""
                    log.debug("Token captured from localStorage")
                    break
            except Exception:
                pass

        await browser.close()

    if not captured.get("token"):
        raise RuntimeError(
            "Could not capture auth token after login. "
            "Check your credentials or LEGION_ORG setting."
        )

    log.info("Browser login succeeded")
    return captured


async def _fill_login_form(page, email: str, password: str) -> None:
    """Best-effort form fill — handles a variety of Legion login layouts."""
    selectors_email = [
        'input[type="email"]',
        'input[name="email"]',
        'input[placeholder*="email" i]',
        'input[id*="email" i]',
    ]
    selectors_password = [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="password" i]',
    ]
    selectors_submit = [
        'button[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("Login")',
        'input[type="submit"]',
    ]

    for sel in selectors_email:
        try:
            await page.fill(sel, email, timeout=3000)
            break
        except Exception:
            continue

    for sel in selectors_password:
        try:
            await page.fill(sel, password, timeout=3000)
            break
        except Exception:
            continue

    for sel in selectors_submit:
        try:
            await page.click(sel, timeout=3000)
            break
        except Exception:
            continue


# ──────────────────────────────────────────────
# Schedule fetching
# ──────────────────────────────────────────────
def fetch_schedule(token: str, employee_id: str, org_id: str) -> list[dict]:
    """
    Fetch shifts from Legion API.
    Returns a list of shift dicts.
    """
    start_date = datetime.now(LOCAL_TZ) - timedelta(weeks=WEEKS_BEHIND)
    end_date   = datetime.now(LOCAL_TZ) + timedelta(weeks=WEEKS_AHEAD)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")

    log.info("Fetching schedule from %s to %s", start_str, end_str)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    # We try several API shapes Legion has used across versions
    endpoints_to_try = [
        # v2 employee schedule
        {
            "method": "GET",
            "url":    f"{API_BASE}/v2/employees/{employee_id}/schedules",
            "params": {"startDate": start_str, "endDate": end_str},
        },
        # v1 my schedule
        {
            "method": "GET",
            "url":    f"{API_BASE}/v1/schedule/mySchedule",
            "params": {"startDate": start_str, "endDate": end_str},
        },
        # org-scoped v2
        {
            "method": "GET",
            "url":    f"{API_BASE}/v2/orgs/{org_id}/employees/{employee_id}/schedules",
            "params": {"startDate": start_str, "endDate": end_str},
        },
        # graphql-style shifts endpoint
        {
            "method": "GET",
            "url":    f"{API_BASE}/v1/shifts",
            "params": {
                "employeeId": employee_id,
                "startDate":  start_str,
                "endDate":    end_str,
            },
        },
        # v2 shifts
        {
            "method": "GET",
            "url":    f"{API_BASE}/v2/shifts",
            "params": {
                "employeeId": employee_id,
                "startDate":  start_str,
                "endDate":    end_str,
            },
        },
    ]

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for ep in endpoints_to_try:
            try:
                log.debug("Trying %s %s", ep["method"], ep["url"])
                resp = client.request(
                    ep["method"],
                    ep["url"],
                    params=ep.get("params"),
                    headers=headers,
                )
                log.debug("Response %s from %s", resp.status_code, ep["url"])
                if resp.status_code == 200:
                    data = resp.json()
                    shifts = _extract_shifts(data)
                    if shifts is not None:
                        log.info("Got %d shift(s) from %s", len(shifts), ep["url"])
                        return shifts
            except Exception as exc:
                log.debug("Error calling %s: %s", ep["url"], exc)

    raise RuntimeError(
        "Could not retrieve schedule from any known Legion API endpoint.\n"
        "Run the script with LOG_LEVEL=DEBUG to see details, then open a GitHub\n"
        "Issue with the endpoint list so we can add your org's specific path."
    )


def _extract_shifts(data: object) -> list[dict] | None:
    """
    Normalise different response shapes into a flat list of shift dicts.
    Returns None if we can't identify shift data in the payload.
    """
    if isinstance(data, list) and data and _looks_like_shift(data[0]):
        return data

    if isinstance(data, dict):
        for key in ("shifts", "schedules", "data", "items", "results", "schedule"):
            val = data.get(key)
            if isinstance(val, list):
                if not val:
                    return []
                if _looks_like_shift(val[0]):
                    return val

    return None


def _looks_like_shift(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    has_time = any(
        k in obj for k in
        ("startTime", "start", "startDate", "shiftStart", "scheduledStart",
         "startTimestamp", "startAt")
    )
    return has_time


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


def parse_shift_time(raw: str | int | None) -> datetime | None:
    """Parse ISO-8601, epoch millis, or epoch seconds into an aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Epoch millis > epoch seconds heuristic
        ts = raw / 1000 if raw > 1e11 else raw
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        raw = raw.strip().rstrip("Z")
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _get_field(shift: dict, *keys) -> object:
    for k in keys:
        v = shift.get(k)
        if v is not None:
            return v
    return None


def shift_to_vevent(shift: dict) -> str | None:
    """Convert one Legion shift dict to a VEVENT string. Returns None if unparseable."""
    start_raw = _get_field(
        shift,
        "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
    )
    end_raw = _get_field(
        shift,
        "endTime", "end", "endDate", "shiftEnd",
        "scheduledEnd", "endTimestamp", "endAt",
    )

    start = parse_shift_time(start_raw)
    end   = parse_shift_time(end_raw)
    if start is None or end is None:
        log.warning("Skipping unparseable shift: %s", shift)
        return None

    # Convert to local time for readability in calendar apps
    start_local = start.astimezone(LOCAL_TZ)
    end_local   = end.astimezone(LOCAL_TZ)

    # Stable UID based on shift content so re-runs never duplicate
    uid_seed = f"{start.isoformat()}-{end.isoformat()}-{shift.get('id', '')}"
    uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:32] + "@legion-ical"

    # Build a useful summary / description
    location = (
        shift.get("locationName")
        or shift.get("location", {}).get("name", "")
        if isinstance(shift.get("location"), dict)
        else shift.get("location", "")
    )
    role = (
        shift.get("roleName")
        or shift.get("role", {}).get("name", "")
        if isinstance(shift.get("role"), dict)
        else shift.get("role", "")
    )
    dept = (
        shift.get("departmentName")
        or shift.get("department", {}).get("name", "")
        if isinstance(shift.get("department"), dict)
        else shift.get("department", "")
    )

    summary_parts = ["RONA Shift"]
    if role:
        summary_parts.append(role)
    if dept:
        summary_parts.append(dept)
    summary = " – ".join(summary_parts)

    # Duration in hours for description convenience
    duration_hrs = (end - start).total_seconds() / 3600
    desc_parts = [f"Duration: {duration_hrs:.1f}h"]
    if location:
        desc_parts.append(f"Location: {location}")
    if role:
        desc_parts.append(f"Role: {role}")
    if dept:
        desc_parts.append(f"Dept: {dept}")
    if shift.get("notes") or shift.get("note"):
        desc_parts.append(f"Notes: {shift.get('notes') or shift.get('note')}")
    description = r"\n".join(desc_parts)

    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dtstart = start_local.strftime("%Y%m%dT%H%M%S")
    dtend   = end_local.strftime("%Y%m%dT%H%M%S")
    tzid    = "America/Toronto"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID={tzid}:{dtstart}",
        f"DTEND;TZID={tzid}:{dtend}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
    ]
    if location:
        lines.append(f"LOCATION:{ical_escape(location)}")
    lines.append("STATUS:CONFIRMED")
    lines.append("TRANSP:OPAQUE")
    lines.append("END:VEVENT")

    return "\n".join(lines)


def ical_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def build_ical(shifts: list[dict]) -> str:
    """Build a complete .ics file string from a list of shift dicts."""
    vevents = []
    seen_uids: set[str] = set()

    for shift in shifts:
        vevent = shift_to_vevent(shift)
        if vevent is None:
            continue
        # Extract UID to deduplicate
        uid_match = re.search(r"UID:(.+)", vevent)
        uid = uid_match.group(1) if uid_match else str(uuid.uuid4())
        if uid in seen_uids:
            log.debug("Deduplicated shift UID %s", uid)
            continue
        seen_uids.add(uid)
        vevents.append(vevent)

    log.info("Writing %d unique events to %s", len(vevents), OUTPUT_FILE)
    return ICAL_HEADER + "\n".join(vevents) + "\n" + ICAL_FOOTER


# ──────────────────────────────────────────────
# Main entrypoint
# ──────────────────────────────────────────────
async def main() -> None:
    dry_run    = "--dry-run"    in sys.argv
    fetch_only = "--fetch-only" in sys.argv

    if not LEGION_EMAIL or not LEGION_PASSWORD:
        sys.exit(
            "ERROR: Set LEGION_EMAIL and LEGION_PASSWORD environment variables.\n"
            "  export LEGION_EMAIL='you@example.com'\n"
            "  export LEGION_PASSWORD='yourpassword'"
        )

    # ── 1. Get auth token ──────────────────────
    cached = load_cached_token() if not fetch_only else None
    if cached:
        token       = cached["token"]
        employee_id = cached["employee_id"]
        org_id      = cached["org_id"]
        log.info("Using cached auth token")
    else:
        # Try fast direct login first
        result = None if fetch_only else try_direct_login(LEGION_EMAIL, LEGION_PASSWORD)

        if result is None:
            # Fall back to browser
            result = await browser_login(LEGION_EMAIL, LEGION_PASSWORD, LEGION_ORG)

        token       = result["token"]
        employee_id = result["employee_id"]
        org_id      = result["org_id"]
        save_token(token, employee_id, org_id)

    # ── 2. Fetch schedule ──────────────────────
    shifts = fetch_schedule(token, employee_id, org_id)

    # ── 3. Generate .ics ───────────────────────
    ical_content = build_ical(shifts)

    if dry_run:
        print(ical_content)
        return

    OUTPUT_FILE.write_text(ical_content, encoding="utf-8")
    log.info("✓ Wrote %s (%d bytes)", OUTPUT_FILE, OUTPUT_FILE.stat().st_size)


if __name__ == "__main__":
    asyncio.run(main())
