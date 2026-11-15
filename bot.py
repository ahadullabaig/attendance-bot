#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""Telegram bot for attendance.py: sends a summary each time you read the portal, and answers
/attendance with the latest numbers.

The bot never contacts the portal itself. Since its October 2026 rebuild, the portal's firewall
bans IPs that look like bots, the bot's server included, and since November it refuses logins
done by an extension. So you log in yourself and click the Chrome extension's icon (extension/),
which sends the pages here, to a small web server on PORT. The bot reads them with
attendance.py's parsers, compares them with your last read and sends the summary.

Needs in .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (the only chat it answers), PUSH_TOKEN (shared
with the extension), optionally PORT=8080. Run with: uv run bot.py
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
PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

STATE_FILE = attendance.DATA_DIR / ".bot-state.json"
STALE_AFTER = timedelta(days=1)
MAX_BODY = 10_000_000               # the dashboard plus every course page is well under 1 MB
SMALL_WORDS = {"and", "of", "on", "the", "for", "in", "to", "with", "&"}
# Table names picked by hand where the automatic rule doesn't give what I'd call the course
NICKNAMES = {"NoSQL Databases": "NoSQL"}
HELP = (
    "/attendance — the latest numbers\n"
    "For fresh ones, log in to the portal in Chrome and click the extension's icon: the summary comes"
    " here, with what changed since your last read."
)
GONE = "/refresh is gone: log in to the portal in Chrome and click the extension's icon for fresh numbers."
# Left over from when the bot kept a daily schedule, or logged in itself, or the extension stopped
# at the security code (needs_you)
OLD_STATE = ("next_keepalive", "session_lost_at", "daily_tried_at", "daily_snapshot", "last_daily",
             "daily_notice", "reminded", "fetch_failed", "refresh_wanted", "needs_you")

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
        # Losing it only costs the cache and the next "since your last read"; crashing would loop forever on Fly
        log("State file unreadable, starting fresh")
        return {"offset": 0}


def save_state(state):
    # Write a copy and rename it into place, so a restart mid-write can't leave half a file
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


def migrate(state):
    """Drop what the daily schedule kept. The last numbers you were sent become the baseline the
    next read is compared with."""
    if "baseline" not in state and "data" in state:
        state["baseline"] = {"data": state["data"], "fetched_at": state["fetched_at"]}
    for key in OLD_STATE:
        state.pop(key, None)
    return state


def short_name(name, width=15):
    """Fit course names in the table: 'Parallel Programming on GPUs' -> 'PPG',
    'Natural Language Processing Laboratory' -> 'NLP Lab'; short names stay as they are,
    unless NICKNAMES says otherwise."""
    if name in NICKNAMES:
        return NICKNAMES[name]
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


def summary(state, changes=(), since=None):
    """The message. With `since` (when the previous read was), it starts with what changed since
    then, or says nothing did."""
    courses = state["data"]["courses"]
    behind = [c for c in courses if advice(c)]
    not_started = [c for c in courses if held(c) == 0]
    on_track = [c for c in courses if c not in behind and c not in not_started]

    # The first line is what the notification shows, so it carries the verdict
    status = (f"{len(behind)} course{'s' if len(behind) > 1 else ''} below {REQUIRED}%" if behind
              else "all on track")
    lines = [f"📊 <b>Attendance</b> — {status}", ""]
    if since and changes:
        lines += [f"🆕 <b>Since your last read</b> ({pretty_time(since)})", *changes, ""]
    elif since:
        lines += [f"No changes since your last read ({pretty_time(since)}).", ""]
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
    """Per-course 'what happened' lines between two reads. Empty only when nothing changed, so an
    empty list can honestly be called "no changes"."""
    if not old:
        return []
    before = {c["code"]: c for c in old["courses"]}
    now = {c["code"] for c in new["courses"]}
    numbers = lambda c: {k: v for k, v in c.items() if k != "detail_url"}  # the link changes, the numbers matter
    lines = []
    for c in new["courses"]:
        prev = before.get(c["code"])
        name = html.escape(c["name"])
        if not prev:
            lines.append(f"• {name}: new on the portal")
            continue
        if "present" not in c or "present" not in prev:
            if ("present" in c) != ("present" in prev):
                lines.append(f"• {name}: attendance details {'now' if 'present' in c else 'no longer'} shown")
            elif c.get("attendance") != prev.get("attendance"):  # only the dashboard's percentage to go on
                lines.append(f"• {name}: {prev.get('attendance', 0):.0f}% → {c.get('attendance', 0):.0f}%")
            elif numbers(c) != numbers(prev):
                lines.append(f"• {name}: changed on the portal")
            continue
        parts = []
        if c["present"] > prev["present"]:
            parts.append(f"+{c['present'] - prev['present']} present")
        elif c["present"] < prev["present"]:
            parts.append(f"present {prev['present']} → {c['present']}")  # corrected on the portal
        missed = [d for d in c["absent_dates"] if d not in prev["absent_dates"]]
        if missed:
            parts.append(f"absent {', '.join(missed)}")
        elif c["absent"] != prev["absent"]:
            parts.append(f"absent {prev['absent']} → {c['absent']}")
        if c["remaining"] != prev["remaining"] and not parts:
            # The semester's plan changed, which moves Skip even with no new classes
            parts.append(f"classes left {prev['remaining']} → {c['remaining']}")
        if not parts and numbers(c) != numbers(prev):
            parts.append("changed on the portal")  # anything else, so "no changes" stays true
        if parts:
            lines.append(f"• {name}: {', '.join(parts)}")
    lines += [f"• {html.escape(c['name'])}: no longer on the portal" for c in old["courses"] if c["code"] not in now]
    return lines


def notify(text):
    """Message the chat from the web server's side, where a Telegram hiccup mustn't lose the push."""
    if not CHAT_ID:
        return
    try:
        send(text)
    except Exception as e:
        log(f"Couldn't message Telegram ({e})")


def fetch_failed(reason):
    """A read that went wrong: say so in the chat."""
    log(f"The extension's portal read failed: {reason}")
    notify(f"⚠️ Couldn't read your attendance from the portal: {html.escape(reason)}\n"
           "Click the extension's icon on a portal page to try again.")


def accept(state, data):
    """Store a read and send the summary, compared with the last read whose summary you got.
    Returns whether it was sent. The baseline only moves once it has been: if Telegram was down,
    the next read is compared with the same numbers, so no change goes unmentioned."""
    now = datetime.now()
    state.update(data=data, fetched_at=now.isoformat(timespec="seconds"))
    save_state(state)
    log(f"Got attendance for {len(data['courses'])} courses from the extension")
    if not CHAT_ID:
        return False
    base = state.get("baseline")
    try:
        if base:
            send(summary(state, changes(base["data"], data), since=datetime.fromisoformat(base["fetched_at"])))
        else:
            send(summary(state))  # the first read: nothing to compare with
    except Exception as e:
        log(f"Couldn't message Telegram ({e}); the next read is compared with the same baseline")
        return False
    state["baseline"] = {"data": data, "fetched_at": state["fetched_at"]}
    save_state(state)
    return True


def on_portal(state, body):
    # The portal addresses to read, from the bot, which also checks the token before the extension
    # touches the portal. (Versions before 3.0 asked /due instead; it's gone, so they stay idle.)
    return {"dashboard_url": attendance.DASHBOARD_URL, "referrer": attendance.LOGIN_URL}


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
            raise attendance.PortalError(f"The page the extension sent isn't the dashboard (saved to {path})")
        need = [c["detail_url"] for c in data["courses"] if c["detail_url"] and c["detail_url"] not in pages]
        if need:
            return {"done": False, "need": need}
        for c in data["courses"]:
            if c["detail_url"]:
                attendance.read_course(c, pages[c["detail_url"]])
    except attendance.PortalError as e:
        fetch_failed(str(e))
        return {"done": False, "error": str(e)}
    sent = accept(state, data)
    return {"done": True, "courses": len(data["courses"]), "sent": sent}


def on_failed(state, body):
    fetch_failed(" ".join(str(body.get("error") or "unknown error").split())[:300])
    return {"ok": True}


class PushHandler(BaseHTTPRequestHandler):
    """The Chrome extension's way in: GET /portal gives the addresses to read, POST /push hands
    over the pages and POST /failed reports a read that went wrong. All need the PUSH_TOKEN."""

    timeout = 30  # a stalled client can't hold a thread forever
    GET = {"/portal": on_portal}
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
        send(GONE)
        return

    # Anything else counts as "show my attendance"
    if "data" not in state:
        send("No numbers yet: log in to the portal in Chrome and click the extension's icon.")
        return
    fetched = datetime.fromisoformat(state["fetched_at"])
    stale = ""
    if datetime.now() - fetched >= STALE_AFTER:
        stale = (f"⚠️ These numbers are from {pretty_time(fetched)}. For fresh ones, log in to the portal"
                 " in Chrome and click the extension's icon.\n\n")
    send(stale + summary(state))


def main():
    if not TOKEN:
        sys.exit("Put TELEGRAM_TOKEN=... in .env")
    if len(PUSH_TOKEN) < 20:
        sys.exit("Put PUSH_TOKEN=<a long random string> in .env; the Chrome extension needs the same one."
                 " Make one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")

    me = tg("getMe")
    tg("setMyCommands", commands=[
        {"command": "attendance", "description": "Latest attendance"},
        {"command": "help", "description": "What this bot does"},
    ])

    state = load_state()
    save_state(migrate(state))
    serve(state)
    log(f"Running as @{me['username']}, listening for the extension on port {PORT}"
        + ("" if CHAT_ID else " (TELEGRAM_CHAT_ID not set: only logging who writes)"))

    while True:
        try:
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
