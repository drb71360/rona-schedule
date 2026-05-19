#!/usr/bin/env python3
"""
Legion WFM → iCal Feed Generator  (RONA Enterprise Edition)
Logs into the RONA Legion enterprise portal, fetches your shift schedule,
and writes a standards-compliant .ics file ready for Google Calendar
/ Thunderbird / Apple Calendar subscription.

Portal URL:  https://enterprise.legion.work/legion/?enterprise=rona

Usage
-----
  python scraper.py              # full run: login → fetch → write schedule.ics
  python scraper.py --fetch-only # reuse cached token, skip browser login
  python scraper.py --dry-run    # print shifts to stdout, don't write file

Environment variables (set in shell or GitHub Secrets)
-------------------------------------------------------
  LEGION_EMAIL      your Legion login e-mail
  LEGION_PASSWORD   your Legion password
  LEGION_ORG        enterprise slug from the URL  → rona
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
LEGION_ORG      = os.getenv("LEGION_ORG", "rona")   # enterprise slug → "rona"
WEEKS_AHEAD     = int(os.getenv("LEGION_WEEKS_AHEAD",  "8"))
WEEKS_BEHIND    = int(os.getenv("LEGION_WEEKS_BEHIND", "2"))

TOKEN_CACHE     = Path(".legion_token.json")
OUTPUT_FILE     = Path(os.getenv("ICAL_OUTPUT", "schedule.ics"))
LOCAL_TZ        = ZoneInfo("America/Toronto")

# ── Correct RONA / Legion enterprise URLs ─────
# Portal:  https://enterprise.legion.work/legion/?enterprise=rona
# The app is a hash-router SPA; "#/console/…" is the client-side route.
BASE_URL  = "https://enterprise.legion.work"
APP_PATH  = "/legion"
APP_URL   = f"{BASE_URL}{APP_PATH}"          # https://enterprise.legion.work/legion
API_BASE  = f"{APP_URL}/api"                 # https://enterprise.legion.work/legion/api

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
def try_direct_login(email: str, password: str, org: str) -> dict | None:
    """
    Attempt a direct REST login against common Legion enterprise endpoints.
    Returns token-info dict on success, None on failure.
    """
    endpoints = [
        f"{API_BASE}/v1/authentication/login",
        f"{API_BASE}/v2/authentication/login",
        f"{API_BASE}/v1/auth/login",
        f"{API_BASE}/authentication/login",
        # Some enterprise installs gate by org at the API level too
        f"{API_BASE}/v1/enterprise/{org}/login",
    ]
    payload = {"email": email, "password": password, "enterprise": org}
    headers = {
        "Content-Type": "application/json",
        "Accept":        "application/json",
        "Origin":        BASE_URL,
        "Referer":       f"{APP_URL}/?enterprise={org}",
    }

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
                        employee_id = str(
                            data.get("employeeId")
                            or (data.get("employee") or {}).get("id", "")
                        )
                        org_id = str(
                            data.get("orgId")
                            or (data.get("org") or {}).get("id", "")
                        )
                        log.info("Direct login succeeded (%s)", url)
                        return {"token": token, "employee_id": employee_id, "org_id": org_id}
                log.debug("Direct login %s → HTTP %s", url, resp.status_code)
            except Exception as exc:
                log.debug("Direct login error at %s: %s", url, exc)
    return None


# ──────────────────────────────────────────────
# Playwright browser login (reliable fallback)
# ──────────────────────────────────────────────
async def browser_login(email: str, password: str, org: str) -> dict:
    """
    Open the RONA Legion enterprise login page in a headless browser,
    sign in, and capture the bearer token from XHR responses or localStorage.

    Login URL:  https://enterprise.legion.work/legion/?enterprise=rona
    """
    log.info("Starting browser-based login …")

    # This is the exact URL format Dave uses — org passed as query param
    login_url = f"{APP_URL}/?enterprise={org}"
    log.info("Navigating to %s", login_url)

    captured: dict = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-CA,en;q=0.9",
            },
        )
        page = await context.new_page()

        # ── Intercept API responses to grab the auth token ──────────
        async def on_response(response):
            if captured:
                return
            url = response.url
            if any(kw in url for kw in ("login", "auth", "authentication", "token", "signin")):
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
                            log.info("Auth token captured from network response: %s", url)
                except Exception:
                    pass

        page.on("response", on_response)

        # Navigate to the login page
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            log.warning("Initial navigation warning (may be fine for SPA): %s", exc)

        # Give the SPA a moment to render the login form
        await asyncio.sleep(3)

        # Fill credentials
        await _fill_login_form(page, email, password)

        # Wait up to 45 seconds for token capture or localStorage update
        for i in range(45):
            await asyncio.sleep(1)

            if captured:
                break

            # ── Check localStorage (SPA apps often store tokens here) ─
            try:
                ls_data = await page.evaluate("""() => {
                    const keys = ['token', 'authToken', 'jwt', 'access_token',
                                  'legion_token', 'legion_auth', 'userToken'];
                    for (const k of keys) {
                        const v = localStorage.getItem(k);
                        if (v) return {token: v, key: k};
                    }
                    // Also scan all localStorage keys for anything JWT-shaped
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        const v = localStorage.getItem(k);
                        if (v && v.startsWith('ey') && v.split('.').length === 3) {
                            return {token: v, key: k};
                        }
                    }
                    return null;
                }""")
                if ls_data:
                    captured["token"]       = ls_data["token"]
                    captured["employee_id"] = await page.evaluate(
                        "() => localStorage.getItem('employeeId') || "
                        "localStorage.getItem('legion_employee_id') || ''"
                    ) or ""
                    captured["org_id"] = await page.evaluate(
                        "() => localStorage.getItem('orgId') || "
                        "localStorage.getItem('legion_org_id') || ''"
                    ) or ""
                    log.info("Auth token captured from localStorage (key: %s)", ls_data["key"])
                    break
            except Exception as exc:
                log.debug("localStorage check error at t+%ds: %s", i, exc)

            # ── Check sessionStorage too ─────────────────────────────
            try:
                ss_data = await page.evaluate("""() => {
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const k = sessionStorage.key(i);
                        const v = sessionStorage.getItem(k);
                        if (v && v.startsWith('ey') && v.split('.').length === 3) {
                            return {token: v, key: k};
                        }
                    }
                    return null;
                }""")
                if ss_data:
                    captured["token"]       = ss_data["token"]
                    captured["employee_id"] = ""
                    captured["org_id"]      = ""
                    log.info("Auth token captured from sessionStorage (key: %s)", ss_data["key"])
                    break
            except Exception:
                pass

        # ── Debug: log page URL and title if still nothing ───────────
        if not captured:
            try:
                current_url   = page.url
                current_title = await page.title()
                log.warning("No token found after 45s. URL: %s | Title: %s",
                            current_url, current_title)
                # Grab any cookies that look like auth tokens
                cookies = await context.cookies()
                for c in cookies:
                    if any(kw in c["name"].lower()
                           for kw in ("token", "auth", "jwt", "session")):
                        captured["token"]       = c["value"]
                        captured["employee_id"] = ""
                        captured["org_id"]      = ""
                        log.info("Auth token found in cookie: %s", c["name"])
                        break
            except Exception as exc:
                log.debug("Debug info error: %s", exc)

        await browser.close()

    if not captured.get("token"):
        raise RuntimeError(
            "Could not capture auth token after login.\n"
            "Re-run with LOG_LEVEL=DEBUG for details.\n"
            "Common fixes:\n"
            "  • LEGION_EMAIL or LEGION_PASSWORD is wrong\n"
            "  • LEGION_ORG should be 'rona' (from ?enterprise=rona in the URL)\n"
            "  • The portal may use SSO / Windows auth — contact IT"
        )

    log.info("Browser login succeeded (employee_id=%s)", captured.get("employee_id") or "unknown")
    return captured


async def _fill_login_form(page, email: str, password: str) -> None:
    """Best-effort form fill — handles Legion SPA login layouts."""

    # Legion enterprise SPA uses hash-routing; the login form may be at #/login
    # Try clicking it if we landed on a splash/redirect page
    try:
        await page.wait_for_selector(
            'input[type="email"], input[name="email"], input[type="text"]',
            timeout=10_000,
        )
    except Exception:
        log.warning("Email field not found within 10s — page may still be loading")

    selectors_email = [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="username" i]',
        'input[id*="email" i]',
        'input[type="text"]',
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
        'button:has-text("Sign In")',
        'input[type="submit"]',
    ]

    filled_email = False
    for sel in selectors_email:
        try:
            await page.fill(sel, email, timeout=3_000)
            log.debug("Filled email with selector: %s", sel)
            filled_email = True
            break
        except Exception:
            continue

    if not filled_email:
        log.warning("Could not fill email field — login may fail")

    for sel in selectors_password:
        try:
            await page.fill(sel, password, timeout=3_000)
            log.debug("Filled password with selector: %s", sel)
            break
        except Exception:
            continue

    for sel in selectors_submit:
        try:
            await page.click(sel, timeout=3_000)
            log.debug("Clicked submit with selector: %s", sel)
            break
        except Exception:
            continue


# ──────────────────────────────────────────────
# Schedule fetching
# ──────────────────────────────────────────────
def fetch_schedule(token: str, employee_id: str, org_id: str) -> list[dict]:
    """
    Fetch shifts from the Legion enterprise API.
    Tries multiple endpoint patterns to handle different org configurations.
    """
    start_date = datetime.now(LOCAL_TZ) - timedelta(weeks=WEEKS_BEHIND)
    end_date   = datetime.now(LOCAL_TZ) + timedelta(weeks=WEEKS_AHEAD)
    start_str  = start_date.strftime("%Y-%m-%d")
    end_str    = end_date.strftime("%Y-%m-%d")

    log.info("Fetching schedule %s → %s", start_str, end_str)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "Origin":        BASE_URL,
        "Referer":       f"{APP_URL}/?enterprise={LEGION_ORG}",
    }

    # Build the list of endpoints to try, most-likely first
    endpoints = []

    if employee_id:
        endpoints += [
            {
                "url":    f"{API_BASE}/v2/employees/{employee_id}/schedules",
                "params": {"startDate": start_str, "endDate": end_str},
            },
            {
                "url":    f"{API_BASE}/v1/employees/{employee_id}/schedules",
                "params": {"startDate": start_str, "endDate": end_str},
            },
        ]
        if org_id:
            endpoints += [
                {
                    "url":    f"{API_BASE}/v2/orgs/{org_id}/employees/{employee_id}/schedules",
                    "params": {"startDate": start_str, "endDate": end_str},
                },
            ]

    endpoints += [
        {
            "url":    f"{API_BASE}/v1/schedule/mySchedule",
            "params": {"startDate": start_str, "endDate": end_str},
        },
        {
            "url":    f"{API_BASE}/v2/schedule/mySchedule",
            "params": {"startDate": start_str, "endDate": end_str},
        },
        {
            "url":    f"{API_BASE}/v1/shifts",
            "params": {"employeeId": employee_id, "startDate": start_str, "endDate": end_str},
        },
        {
            "url":    f"{API_BASE}/v2/shifts",
            "params": {"employeeId": employee_id, "startDate": start_str, "endDate": end_str},
        },
        {
            "url":    f"{API_BASE}/v1/myShifts",
            "params": {"startDate": start_str, "endDate": end_str},
        },
        # Enterprise-specific path guesses
        {
            "url":    f"{API_BASE}/v1/enterprise/{LEGION_ORG}/mySchedule",
            "params": {"startDate": start_str, "endDate": end_str},
        },
    ]

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for ep in endpoints:
            try:
                log.debug("Trying GET %s", ep["url"])
                resp = client.get(ep["url"], params=ep.get("params"), headers=headers)
                log.debug("  → HTTP %s", resp.status_code)

                if resp.status_code == 200:
                    data = resp.json()
                    shifts = _extract_shifts(data)
                    if shifts is not None:
                        log.info("Got %d shift(s) from %s", len(shifts), ep["url"])
                        return shifts

                elif resp.status_code == 401:
                    log.warning("401 Unauthorised — token may be invalid or expired")
                    break   # No point trying other endpoints with a bad token

            except Exception as exc:
                log.debug("Error calling %s: %s", ep["url"], exc)

    raise RuntimeError(
        "Could not retrieve schedule from any known Legion API endpoint.\n"
        "Re-run with LOG_LEVEL=DEBUG to see which paths were tried.\n"
        "If you see 401 errors, your auth token expired — delete "
        ".legion_token.json and re-run."
    )


def _extract_shifts(data: object) -> list[dict] | None:
    """Normalise different Legion response shapes into a flat list of shift dicts."""
    if isinstance(data, list):
        if not data:
            return []
        if _looks_like_shift(data[0]):
            return data

    if isinstance(data, dict):
        for key in ("shifts", "schedules", "data", "items", "results", "schedule",
                    "myShifts", "assignedShifts"):
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
    return any(k in obj for k in (
        "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
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
X-WR-CALDESC:Auto-generated from Legion WFM (enterprise.legion.work)
REFRESH-INTERVAL;VALUE=DURATION:PT4H
X-PUBLISHED-TTL:PT4H
"""

ICAL_FOOTER = "END:VCALENDAR\n"


def parse_shift_time(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
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
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _get(shift: dict, *keys):
    for k in keys:
        v = shift.get(k)
        if v is not None:
            return v
    return None


def _name_from(field) -> str:
    if isinstance(field, dict):
        return field.get("name", "")
    return str(field) if field else ""


def shift_to_vevent(shift: dict) -> str | None:
    start = parse_shift_time(_get(
        shift, "startTime", "start", "startDate", "shiftStart",
        "scheduledStart", "startTimestamp", "startAt",
    ))
    end = parse_shift_time(_get(
        shift, "endTime", "end", "endDate", "shiftEnd",
        "scheduledEnd", "endTimestamp", "endAt",
    ))
    if start is None or end is None:
        log.warning("Skipping shift with unparseable times: %s", shift)
        return None

    start_local = start.astimezone(LOCAL_TZ)
    end_local   = end.astimezone(LOCAL_TZ)

    # Stable, content-derived UID — guarantees no duplicates across runs
    uid_seed = f"{start.isoformat()}|{end.isoformat()}|{shift.get('id', '')}"
    uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:32] + "@legion-rona"

    location = _name_from(_get(shift, "location", "locationName"))
    role     = _name_from(_get(shift, "role", "roleName", "position", "positionName"))
    dept     = _name_from(_get(shift, "department", "departmentName"))

    summary_parts = ["RONA Shift"]
    if role:
        summary_parts.append(role)
    if dept:
        summary_parts.append(dept)
    summary = " – ".join(summary_parts)

    duration_hrs = (end - start).total_seconds() / 3600
    desc_parts = [f"Duration: {duration_hrs:.1f}h"]
    if location:
        desc_parts.append(f"Location: {location}")
    if role:
        desc_parts.append(f"Role: {role}")
    if dept:
        desc_parts.append(f"Dept: {dept}")
    note = _get(shift, "notes", "note", "comment")
    if note:
        desc_parts.append(f"Notes: {note}")
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
        f"SUMMARY:{ical_escape(summary)}",
        f"DESCRIPTION:{description}",
    ]
    if location:
        lines.append(f"LOCATION:{ical_escape(location)}")
    lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT"]
    return "\n".join(lines)


def ical_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def build_ical(shifts: list[dict]) -> str:
    vevents   = []
    seen_uids: set[str] = set()

    for shift in shifts:
        vevent = shift_to_vevent(shift)
        if vevent is None:
            continue
        uid_match = re.search(r"UID:(.+)", vevent)
        uid = uid_match.group(1) if uid_match else str(uuid.uuid4())
        if uid in seen_uids:
            log.debug("Duplicate UID skipped: %s", uid)
            continue
        seen_uids.add(uid)
        vevents.append(vevent)

    log.info("Writing %d unique event(s) → %s", len(vevents), OUTPUT_FILE)
    return ICAL_HEADER + "\n".join(vevents) + "\n" + ICAL_FOOTER


# ──────────────────────────────────────────────
# Main
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

    log.info("RONA Legion iCal Scraper starting …")
    log.info("Portal: %s/?enterprise=%s", APP_URL, LEGION_ORG)

    # ── 1. Authenticate ───────────────────────
    cached = load_cached_token() if not fetch_only else None
    if cached:
        token, employee_id, org_id = (
            cached["token"], cached["employee_id"], cached["org_id"]
        )
        log.info("Using cached auth token")
    else:
        result = None if fetch_only else try_direct_login(LEGION_EMAIL, LEGION_PASSWORD, LEGION_ORG)
        if result is None:
            result = await browser_login(LEGION_EMAIL, LEGION_PASSWORD, LEGION_ORG)
        token       = result["token"]
        employee_id = result["employee_id"]
        org_id      = result["org_id"]
        save_token(token, employee_id, org_id)

    # ── 2. Fetch shifts ───────────────────────
    shifts = fetch_schedule(token, employee_id, org_id)

    # ── 3. Generate iCal ──────────────────────
    ical_content = build_ical(shifts)

    if dry_run:
        print(ical_content)
        return

    OUTPUT_FILE.write_text(ical_content, encoding="utf-8")
    log.info("✓ Done — %s written (%d bytes)", OUTPUT_FILE, OUTPUT_FILE.stat().st_size)


if __name__ == "__main__":
    asyncio.run(main())
