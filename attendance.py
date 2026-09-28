#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""Print your attendance from the MSRIT parents portal without logging in by hand.

Usage:
    uv run attendance.py            # table in the terminal
    uv run attendance.py --json     # machine-readable
    uv run attendance.py --show     # visible browser, e.g. to solve a captcha puzzle
    uv run attendance.py --dump     # also save raw pages to debug/ for development
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = "https://parents.msrit.edu/newparents/"
LOGIN_URL = BASE + "index.php"
DASHBOARD_URL = BASE + "index.php?option=com_studentdashboard&controller=studentdashboard&task=dashboard"

HERE = Path(__file__).resolve().parent
# Session, browser profile and debug pages; on Fly.io this is the mounted volume
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE))
PROFILE_DIR = DATA_DIR / ".browser-profile"
SESSION_FILE = DATA_DIR / ".session.json"
DEBUG_DIR = DATA_DIR / "debug"
# Installed Google Chrome locally; the container uses Playwright's own Chromium
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome")

# Portal rules (from the "Attendance" info box on the dashboard)
REQUIRED = 85
CONDONABLE = 75


class PortalError(Exception):
    """The portal didn't give us what we expected; the message says what and where the page was saved."""


def load_env():
    """Read .env into os.environ (real environment variables win)."""
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_credentials():
    load_env()
    missing = [k for k in ("USN", "DOB", "FATHER_LAST4") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)} (see .env.example)")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", os.environ["DOB"]):
        sys.exit("DOB in .env must look like YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}", os.environ["FATHER_LAST4"]):
        sys.exit("FATHER_LAST4 in .env must be exactly 4 digits")
    return os.environ["USN"], os.environ["DOB"], os.environ["FATHER_LAST4"]


def save_html(name, html):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"{name}.html").write_text(html)
    return DEBUG_DIR / f"{name}.html"


def dump(page, name):
    path = save_html(name, page.content())
    page.screenshot(path=DEBUG_DIR / f"{name}.png", full_page=True)
    return path


def login(page, usn, dob, last4, captcha_timeout):
    yyyy, mm, dd = dob.split("-")

    # Step 1: USN + date of birth
    page.goto(LOGIN_URL)
    page.fill("#username", usn)
    page.select_option("#dd", label=dd)  # option values carry a trailing space, labels don't
    page.select_option("#mm", value=mm)
    page.select_option("#yyyy", value=yyyy)
    page.evaluate("putdate()")  # the page builds the hidden passwd field from the dropdowns

    # The invisible reCAPTCHA runs on page load and fills this field when it passes.
    # If Google wants a puzzle instead, it only shows in a visible browser (--show).
    try:
        page.wait_for_function(
            "document.getElementById('captcha-response').value !== ''",
            timeout=captcha_timeout * 1000,
        )
    except PWTimeout:
        path = dump(page, "captcha-timeout")
        raise PortalError(f"reCAPTCHA didn't pass. Run attendance.py --show and solve it. (page saved to {path})")
    page.click('input[type=submit][value="Login"]')
    page.wait_for_load_state()

    # Step 2: verification type + last 4 digits of father's mobile
    try:
        select = page.locator("select:visible").first
        select.wait_for(timeout=15_000)
        father = select.evaluate("s => [...s.options].find(o => /father/i.test(o.text))?.value")
        if father is None:
            raise PortalError("no 'Father Mobile' option on the verification page")
        select.select_option(value=father)
        form = page.locator("form", has=select)
        boxes = form.locator("input:visible:not([type=submit]):not([type=button]):not([type=hidden])")
        if boxes.count() == 4:
            for box, digit in zip(boxes.all(), last4):
                box.press_sequentially(digit)
        else:
            boxes.first.press_sequentially(last4)
        form.get_by_role("button", name=re.compile("submit", re.I)).click()
        page.wait_for_load_state()
    except (PWTimeout, PortalError) as e:
        path = dump(page, "verification-failed")
        raise PortalError(f"Verification step failed: {e} (page saved to {path})") from e


def parse_dashboard(html):
    soup = BeautifulSoup(html, "html.parser")
    caption = soup.find("caption", string=re.compile("attendance status", re.I))
    if caption is None:
        return None
    courses = []
    for row in caption.find_parent("table").select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        pct = re.search(r"\d+(?:\.\d+)?", cells[4].get_text(strip=True))
        link = cells[4].find("a", href=True)
        courses.append({
            "code": cells[0].get_text(strip=True),
            "name": cells[1].get_text(strip=True),
            "attendance": float(pct.group()) if pct else None,
            "detail_url": urljoin(LOGIN_URL, link["href"]) if link else None,
        })
    updated = soup.select_one(".cn-last-update")
    return {
        "last_updated": updated.get_text(strip=True).split(":", 1)[-1].strip() if updated else None,
        "courses": courses,
    }


def parse_detail(html):
    """Present / absent / still-to-go counts and absent dates from a course's attendance page."""
    soup = BeautifulSoup(html, "html.parser")
    counts = {}
    for key, cls in (("present", "cn-attend"), ("absent", "cn-absent"), ("remaining", "cn-still")):
        label = soup.select_one(f".cn-legend .{cls}")
        if label is None:
            return None
        number = re.search(r"\d+", label.get_text())  # "Present[]" means none recorded yet
        counts[key] = int(number.group()) if number else 0
    counts["absent_dates"] = [
        row.find_all("td")[1].get_text(strip=True)
        for row in soup.select("table.cn-attend-list2 tbody tr")
        if len(row.find_all("td")) > 1
    ]
    return counts


def skip_budget(present, absent, remaining):
    """How many of the remaining classes can be missed while still ending the semester at REQUIRED%.
    Negative means the target is out of reach even with full attendance."""
    total = present + absent + remaining
    needed = -(-REQUIRED * total // 100)  # ceil without floats
    return remaining - max(0, needed - present)


def catch_up(present, held):
    """Classes in a row needed to be back at REQUIRED% right now (CIEs check the current figure)."""
    return max(0, -(-(REQUIRED * held - 100 * present) // (100 - REQUIRED)))


def verdict(c):
    """One-line summary of where a course stands, plus a level: ok / warn / bad / none."""
    if "present" not in c:
        return "?", "warn"
    held = c["present"] + c["absent"]
    if held == 0:
        return "no classes yet", "none"
    skip, left = c["can_skip"], c["remaining"]
    if skip < 0:
        best = round(100 * (c["present"] + left) / (held + left))
        return f"max {best}% even attending all", "bad"
    behind = catch_up(c["present"], held)
    if behind:
        return f"attend next {behind} to reach {REQUIRED}% · can skip {skip} of {left} overall", "warn"
    if skip == 0:
        return f"attend all {left}", "warn"
    return f"can skip {skip} of {left}", "ok"


def browser_login(show):
    """Log in with a real browser (needed for the captcha). Returns the dashboard HTML and URL, plus
    the browser's User-Agent and cookies so plain HTTP requests can carry on the same session."""
    usn, dob, last4 = load_credentials()
    with sync_playwright() as p:
        # A persistent profile keeps Google's cookies, which makes the invisible captcha happier
        context = p.chromium.launch_persistent_context(PROFILE_DIR, channel=BROWSER_CHANNEL, headless=not show)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            login(page, usn, dob, last4, captcha_timeout=180 if show else 30)
            return page.content(), page.url, page.evaluate("navigator.userAgent"), context.cookies(BASE)
        finally:
            context.close()


def http_session(user_agent, cookies):
    http = requests.Session()
    http.headers["User-Agent"] = user_agent  # the session is tied to the browser that logged in
    for c in cookies:
        http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return http


def load_session():
    try:
        saved = json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return saved if isinstance(saved, dict) else None  # older versions saved a bare cookie list


def save_session(http):
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in http.cookies]
    SESSION_FILE.write_text(json.dumps({"user_agent": http.headers["User-Agent"], "cookies": cookies}))
    SESSION_FILE.chmod(0o600)


def session_alive():
    """Touch the portal with the saved session: stops it idling out, and says whether it still works."""
    saved = load_session()
    if not saved:
        return False
    http = http_session(saved["user_agent"], saved["cookies"])
    resp = http.get(DASHBOARD_URL, headers={"Referer": LOGIN_URL}, timeout=30)
    return parse_dashboard(resp.text) is not None


def fetch_attendance(show=False, dump_pages=False):
    """Return dashboard + per-course attendance. Only logging in needs a browser; with a live
    session everything is plain HTTP, which is much faster on a small server.

    The portal's session cookie dies with the browser, so we save it ourselves. Every request must
    carry a Referer from the portal: without one the portal logs you out on the spot."""
    data = None
    saved = load_session()
    if saved:
        http = http_session(saved["user_agent"], saved["cookies"])
        resp = http.get(DASHBOARD_URL, headers={"Referer": LOGIN_URL}, timeout=30)
        dashboard_html, dashboard_url = resp.text, resp.url
        data = parse_dashboard(dashboard_html)

    if data is None:
        dashboard_html, dashboard_url, user_agent, cookies = browser_login(show)
        data = parse_dashboard(dashboard_html)
        if data is None:
            path = save_html("no-dashboard", dashboard_html)
            raise PortalError(f"Logged in but couldn't find the attendance table. Page saved to {path}")
        http = http_session(user_agent, cookies)
    if dump_pages:
        save_html("dashboard", dashboard_html)

    for c in data["courses"]:
        if not c["detail_url"]:
            continue
        resp = http.get(c["detail_url"], headers={"Referer": dashboard_url}, timeout=30)
        detail = parse_detail(resp.text)
        if detail is None:
            path = save_html(f"attendance-{c['code']}", resp.text)
            raise PortalError(f"Couldn't read the attendance page for {c['code']}. Page saved to {path}")
        if dump_pages and not (DEBUG_DIR / "attendance-detail.html").exists():
            save_html("attendance-detail", resp.text)
        c.update(detail, can_skip=skip_budget(detail["present"], detail["absent"], detail["remaining"]))

    save_session(http)
    return data


def print_table(data):
    colors = {"ok": "32", "warn": "33", "bad": "31", "none": "2"} if sys.stdout.isatty() else {}
    print(f"Attendance — portal last updated {data['last_updated'] or 'unknown'}"
          f" · target {REQUIRED}% by end of semester\n")
    for c in data["courses"]:
        name = c["name"] if len(c["name"]) <= 34 else c["name"][:33] + "…"
        pct = "  -" if c["attendance"] is None else f"{c['attendance']:3.0f}"
        held = f"{c['present']}/{c['present'] + c['absent']}" if "present" in c else ""
        label, level = verdict(c)
        if level in colors:
            label = f"\033[{colors[level]}m{label}\033[0m"
        print(f"  {c['code']:<10} {name:<34} {pct}%  {held:>6}  {label}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--show", action="store_true", help="open a visible browser window")
    parser.add_argument("--dump", action="store_true", help="save dashboard + one attendance page to debug/")
    args = parser.parse_args()

    try:
        data = fetch_attendance(show=args.show, dump_pages=args.dump)
    except PortalError as e:
        sys.exit(str(e))

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_table(data)


if __name__ == "__main__":
    main()
