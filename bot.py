#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""Telegram bot for attendance.py: answers /attendance and sends a daily summary.

The bot never contacts the portal itself. Since its October 2026 rebuild, the portal's firewall
bans IPs that look like bots, the bot's server included. Instead, the Chrome extension in
extension/ logs in from your own browser once a day and sends the pages here, to a small web
server on PORT. The bot reads them with attendance.py's parsers and does the rest.

Needs in .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (the only chat it answers), PUSH_TOKEN (shared
with the extension), optionally DAILY_AT=19:00 and PORT=8080. Run with: uv run bot.py
"""

import hmac
import html
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import attendance
from attendance import REQUIRED, catch_up

attendance.load_env()
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DAILY_AT = os.environ.get("DAILY_AT", "19:00")
PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

STATE_FILE = attendance.DATA_DIR / ".bot-state.json"
FETCH_LEAD = timedelta(minutes=30)  # the extension checks the portal this long before the daily summary
RETRY_AFTER = timedelta(hours=1)    # after a failed check
MAX_TRIES = 2                       # automatic checks per day: a broken login shouldn't retry all evening
NOTICE_AFTER = timedelta(hours=3)   # this long past DAILY_AT without today's numbers, say so once
STALE_AFTER = timedelta(days=1)
MAX_BODY = 10_000_000               # the dashboard plus every course page is well under 1 MB
SMALL_WORDS = {"and", "of", "on", "the", "for", "in", "to", "with", "&"}
HELP = (
    "/attendance — the latest numbers from your laptop\n"
    "/refresh — ask your laptop to check the portal now (Chrome needs to be open)\n"
    f"A summary also arrives every day at {DAILY_AT}, once your laptop has checked the portal."
)

# The web server's threads and the Telegram loop share the state dict; one of them at a time
LOCK = threading.Lock()


def log(*args):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def tg(method, **params):
    body = {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    with urllib.request.urlopen(url, urllib.parse.urlencode(body).encode(), timeout=70) as resp:
        return json.load(resp)["result"]


def send(text):
    return tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"offset": 0}
    except json.JSONDecodeError:
        # Losing it only costs the cache and the next daily diff; crashing would loop forever on Fly
        log("State file unreadable, starting fresh")
        return {"offset": 0}


def save_state(state):
    # Write a copy and rename it into place, so a restart mid-write can't leave half a file
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


def at_time(hhmm, day):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute)


def fetch_time(now):
    """When the latest daily check fell due: FETCH_LEAD before today's summary, or before
    yesterday's if today's is still ahead."""
    due = at_time(DAILY_AT, now) - FETCH_LEAD
    return due if due <= now else due - timedelta(days=1)


def checked_since(state, when):
    fetched = state.get("fetched_at")
    return bool(fetched) and datetime.fromisoformat(fetched) >= when


def failures_since(state, when):
    return [t for t in map(datetime.fromisoformat, state.get("fetch_failed", [])) if t >= when]


def fetch_due(state, now):
    """Whether the extension should check the portal now. The portal updates once a day, so one
    check a day is enough, plus one for each /refresh. After a failure, wait an hour and try once
    more, then leave it until tomorrow: every try is a login from your own connection."""
    if state.get("refresh_wanted"):
        return True
    since = fetch_time(now)
    if checked_since(state, since):
        return False
    failed = failures_since(state, since)
    return len(failed) < MAX_TRIES and (not failed or now - max(failed) >= RETRY_AFTER)


def short_name(name, width=15):
    """Fit course names in the table: 'Parallel Programming on GPUs' -> 'PPG',
    'Natural Language Processing Laboratory' -> 'NLP Lab'; short names stay as they are."""
    words = name.split()
    lab = len(words) > 1 and words[-1] in ("Laboratory", "Lab")
    if lab:
        words = words[:-1]
    base = " ".join(words)
    if len(words) >= 3 or len(base) + 4 * lab > width:
        initials = "".join(w[0].upper() for w in words if w.lower() not in SMALL_WORDS)
        base = initials if len(initials) >= 2 else base[: width - 1 - 4 * lab] + "…"
    return base + " Lab" * lab


def held(c):
    return c.get("present", 0) + c.get("absent", 0)


def advice(c):
    """What to do about a course that's below REQUIRED% right now; None when it's fine."""
    if held(c) == 0:
        return None
    if c["can_skip"] < 0:
        best = round(100 * (c["present"] + c["remaining"]) / (held(c) + c["remaining"]))
        return f"can reach at most {best}% now, even attending everything"
    n = catch_up(c["present"], held(c))
    if n:
        return f"attend the next {n} class{'es' if n > 1 else ''} to get back to {REQUIRED}%"
    return None


def pretty_day(text):
    try:
        return datetime.strptime(text, "%d/%m/%Y").strftime("%-d %b")
    except (TypeError, ValueError):
        return text or "?"


def pretty_time(when):
    return when.strftime("%H:%M" if when.date() == date.today() else "%-d %b, %H:%M")


def summary(state, changes=()):
    courses = state["data"]["courses"]
    behind = [c for c in courses if advice(c)]
    not_started = [c for c in courses if held(c) == 0]
    on_track = [c for c in courses if c not in behind and c not in not_started]

    # The first line is what the notification shows, so it carries the verdict
    status = (f"{len(behind)} course{'s' if len(behind) > 1 else ''} below {REQUIRED}%" if behind
              else "all on track")
    lines = [f"📊 <b>Attendance</b> — {status}", ""]
    if changes:
        lines += ["🆕 <b>Since last summary</b>", *changes, ""]
    for c in behind:
        lines.append(f"⚠️ <b>{html.escape(c['name'])}</b> is at {c['attendance']:.0f}% — {advice(c)}")
    if behind:
        lines.append("")

    rows = [f"{'Course':<15}{'%':>4}{'Came':>7}{'Skip':>5}"]
    for c in behind + on_track:
        came = f"{c['present']}/{held(c)}"
        rows.append(f"{short_name(c['name']):<15}{c['attendance']:>4.0f}{came:>7}{max(c['can_skip'], 0):>5}")
    rows += [f"{short_name(c['name']):<15}{'–':>4}{'–':>7}{'–':>5}" for c in not_started]

    lines += [
        "<pre>" + html.escape("\n".join(rows)) + "</pre>",
        f"<i>Skip = classes you can still miss and end the semester at {REQUIRED}%</i>",
        f"<i>Portal updated {pretty_day(state['data']['last_updated'])}"
        f" · checked {pretty_time(datetime.fromisoformat(state['fetched_at']))}</i>",
    ]
    return "\n".join(lines)


def changes(old, new):
    """Per-course 'what happened' lines between two snapshots."""
    if not old:
        return []
    before = {c["code"]: c for c in old["courses"]}
    lines = []
    for c in new["courses"]:
        prev = before.get(c["code"])
        if not prev or "present" not in c or "present" not in prev:
            continue
        parts = []
        if c["present"] > prev["present"]:
            parts.append(f"+{c['present'] - prev['present']} present")
        missed = [d for d in c["absent_dates"] if d not in prev["absent_dates"]]
        if missed:
            parts.append(f"absent {', '.join(missed)}")
        if parts:
            lines.append(f"• {html.escape(c['name'])}: {', '.join(parts)}")
    return lines


def daily_due(state, now):
    return now >= at_time(DAILY_AT, now) and state.get("last_daily") != now.date().isoformat()


def send_daily(state, now):
    """Send today's summary once today's numbers are in. If they haven't come a few hours after
    DAILY_AT (laptop off, or the check failed), say so once; the summary follows when they arrive."""
    today = now.date().isoformat()
    if checked_since(state, fetch_time(now)):
        diff = changes(state.get("daily_snapshot"), state["data"])
        send(summary(state, diff))
        # Only once it's sent: if Telegram was unreachable, the next try has the same changes
        state.update(last_daily=today, daily_snapshot=state["data"])
        save_state(state)
    elif now >= at_time(DAILY_AT, now) + NOTICE_AFTER and state.get("daily_notice") != today:
        last = (f"Its last check was {pretty_time(datetime.fromisoformat(state['fetched_at']))}."
                if state.get("fetched_at") else "It hasn't sent any numbers yet.")
        send(f"⚠️ No summary yet: your laptop hasn't checked the portal since {fetch_time(now):%H:%M}. {last}\n"
             "The summary comes as soon as it does (Chrome needs to be open). /attendance shows the last numbers.")
        state["daily_notice"] = today
        save_state(state)


def notify(text):
    """Message the chat from the web server's side, where a Telegram hiccup mustn't lose the push."""
    if not CHAT_ID:
        return
    try:
        send(text)
    except Exception as e:
        log(f"Couldn't message Telegram ({e})")


def fetch_failed(state, reason):
    """Record a failed check, so fetch_due() backs off, and say what went wrong."""
    now = datetime.now()
    state["fetch_failed"] = [*state.get("fetch_failed", [])[-9:], now.isoformat(timespec="seconds")]
    asked = state.pop("refresh_wanted", None)  # one try per /refresh
    save_state(state)
    log(f"The extension's portal check failed: {reason}")
    if asked:
        then = "\nSend /refresh to try again."
    elif checked_since(state, fetch_time(now)):
        then = ""  # a manual run on top of today's check; today's numbers are already in
    elif fetch_due(state, now + RETRY_AFTER):
        then = "\nIt'll try again in an hour."
    else:
        then = "\nNo more automatic tries today; send /refresh to try again."
    notify(f"⚠️ Your laptop couldn't check the portal: {html.escape(reason)}{then}")


def accept(state, data):
    now = datetime.now()
    state.update(data=data, fetched_at=now.isoformat(timespec="seconds"))
    state.pop("fetch_failed", None)
    asked = state.pop("refresh_wanted", None)
    save_state(state)
    log(f"Got attendance for {len(data['courses'])} courses from the extension")
    if not CHAT_ID:
        return
    try:
        if daily_due(state, now):
            send_daily(state, now)  # also answers a /refresh
        elif asked:
            send(summary(state))
    except Exception as e:
        log(f"Couldn't message Telegram ({e}); the daily summary will retry")


def on_due(state, body):
    return {"fetch": fetch_due(state, datetime.now()),
            "login_url": attendance.LOGIN_URL, "dashboard_url": attendance.DASHBOARD_URL}


def on_push(state, body):
    """The extension sends the dashboard first and gets back the course pages to fetch, then sends
    them all. That way only attendance.py's parsers need to understand the portal's pages."""
    dashboard, pages = body.get("dashboard"), body.get("pages") or {}
    if not isinstance(dashboard, str) or not isinstance(pages, dict):
        return {"done": False, "error": "Expected the dashboard page and a dict of course pages"}
    try:
        data = attendance.parse_dashboard(dashboard)
        if data is None:
            path = attendance.save_html("pushed-dashboard", dashboard)
            raise attendance.PortalError(f"The page your laptop sent isn't the dashboard (saved to {path})")
        need = [c["detail_url"] for c in data["courses"] if c["detail_url"] and c["detail_url"] not in pages]
        if need:
            return {"done": False, "need": need}
        for c in data["courses"]:
            if c["detail_url"]:
                attendance.read_course(c, pages[c["detail_url"]])
    except attendance.PortalError as e:
        fetch_failed(state, str(e))
        return {"done": False, "error": str(e)}
    accept(state, data)
    return {"done": True, "courses": len(data["courses"])}


def on_failed(state, body):
    reason = " ".join(str(body.get("error") or "unknown error").split())[:300]
    if isinstance(body.get("html"), str):
        reason += f" (page saved to {attendance.save_html('extension-failed', body['html'])})"
    fetch_failed(state, reason)
    return {"ok": True}


class PushHandler(BaseHTTPRequestHandler):
    """The Chrome extension's way in: GET /due asks whether to check the portal, POST /push hands
    over the pages and POST /failed reports a check that went wrong. All need the PUSH_TOKEN."""

    timeout = 30  # a stalled client can't hold a thread forever
    GET = {"/due": on_due}
    POST = {"/push": on_push, "/failed": on_failed}

    def log_message(self, *args):  # http.server logs every request; log() covers what matters
        pass

    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve(self, routes):
        route = routes.get(urllib.parse.urlsplit(self.path).path)
        if route is None:
            return self.answer(404, {"error": "not found"})
        expected = f"Bearer {PUSH_TOKEN}".encode()
        if not PUSH_TOKEN or not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
            return self.answer(401, {"error": "wrong token"})
        body = {}
        if routes is self.POST:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.answer(400, {"error": "bad Content-Length"})
            if length > MAX_BODY:
                return self.answer(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self.answer(400, {"error": "expected JSON"})
            if not isinstance(body, dict):
                return self.answer(400, {"error": "expected a JSON object"})
        with LOCK:
            try:
                reply = route(self.server.state, body)
            except Exception as e:
                log(traceback.format_exc())  # a bug: the extension only gets the gist
                return self.answer(500, {"error": f"{type(e).__name__}: {e}"[:300]})
        self.answer(200, reply)

    def do_GET(self):
        self.serve(self.GET)

    def do_POST(self):
        self.serve(self.POST)


def serve(state, host="0.0.0.0", port=PORT):
    """Start the extension's web server in the background; returns it (tests use port 0)."""
    server = ThreadingHTTPServer((host, port), PushHandler)
    server.daemon_threads = True
    server.state = state
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def handle(message, state):
    chat = message["chat"]
    if str(chat["id"]) != CHAT_ID:
        hint = "" if CHAT_ID else " — if that's you, put TELEGRAM_CHAT_ID=<id> in .env"
        log(f"Ignoring message from chat {chat['id']} (@{chat.get('username')}){hint}")
        return
    words = (message.get("text") or "").strip().lower().split()
    command = words[0].split("@")[0] if words else ""
    if command in ("/start", "/help"):
        send(HELP)
        return
    if command == "/refresh":
        state["refresh_wanted"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        send("⏳ Asked your laptop to check the portal. If Chrome is open, the numbers come here"
             " within a few minutes.")
        return

    # Anything else counts as "show my attendance"
    if "data" not in state:
        send("No numbers yet: your laptop hasn't sent any. Send /refresh with Chrome open.")
        return
    fetched = datetime.fromisoformat(state["fetched_at"])
    stale = ""
    if datetime.now() - fetched >= STALE_AFTER:
        stale = (f"⚠️ These numbers are from {pretty_time(fetched)}: your laptop hasn't checked"
                 " the portal since.\n\n")
    send(stale + summary(state))


def main():
    if not TOKEN:
        sys.exit("Put TELEGRAM_TOKEN=... in .env")
    try:
        datetime.strptime(DAILY_AT, "%H:%M")
    except ValueError:
        sys.exit("DAILY_AT in .env must look like 19:00")
    if len(PUSH_TOKEN) < 20:
        sys.exit("Put PUSH_TOKEN=<a long random string> in .env; the Chrome extension needs the same one."
                 " Make one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")

    me = tg("getMe")
    tg("setMyCommands", commands=[
        {"command": "attendance", "description": "Latest attendance"},
        {"command": "refresh", "description": "Ask your laptop to check the portal now"},
        {"command": "help", "description": "What this bot does"},
    ])

    state = load_state()
    for key in ("next_keepalive", "session_lost_at", "daily_tried_at"):  # from when the bot logged in itself
        state.pop(key, None)
    serve(state)
    log(f"Running as @{me['username']}, daily summary at {DAILY_AT}, listening for the extension on port {PORT}"
        + ("" if CHAT_ID else " (TELEGRAM_CHAT_ID not set: only logging who writes)"))

    while True:
        try:
            with LOCK:
                now = datetime.now()
                if CHAT_ID and daily_due(state, now):
                    send_daily(state, now)
            updates = tg("getUpdates", offset=state.get("offset", 0), timeout=50, allowed_updates=["message"])
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"Telegram unreachable ({e}), retrying in 15s")  # offline, or laptop just woke up
            time.sleep(15)
            continue
        for update in updates:
            with LOCK:
                state["offset"] = update["update_id"] + 1
                save_state(state)
                if "message" in update:
                    try:
                        handle(update["message"], state)
                    except Exception:
                        log(traceback.format_exc())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:  # Fly sends SIGINT on deploy/restart
        log("Stopping")
