#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""Telegram bot for attendance.py: answers /attendance and sends a daily summary.

Needs in .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (the only chat it answers),
optionally DAILY_AT=19:00. Run with: uv run bot.py
"""

import html
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import attendance
from attendance import REQUIRED, catch_up

attendance.load_env()
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DAILY_AT = os.environ.get("DAILY_AT", "19:00")

STATE_FILE = attendance.DATA_DIR / ".bot-state.json"
CACHE_FOR = timedelta(hours=3)  # the portal only updates once a day, no need to hit it on every message
KEEPALIVE_EVERY = timedelta(minutes=10)
RELOGIN_AFTER = timedelta(hours=1)  # after losing the session, or after a failed login
SMALL_WORDS = {"and", "of", "on", "the", "for", "in", "to", "with", "&"}
HELP = (
    "/attendance — current attendance (cached up to 3h)\n"
    "/refresh — check the portal right now\n"
    f"A summary also arrives every day at {DAILY_AT}."
)


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


def is_fresh(state):
    fetched = state.get("fetched_at")
    return bool(fetched) and datetime.now() - datetime.fromisoformat(fetched) < CACHE_FOR


def refresh(state):
    state.update(data=attendance.fetch_attendance(), fetched_at=datetime.now().isoformat(timespec="seconds"))
    save_state(state)


def keep_session_warm(state):
    """A browser login takes over a minute on a small server, while a live session answers in
    ~2s. So keep the portal session alive, and when it's gone, log in again in the background
    instead of making a message wait for it.

    The portal allows one session at a time: a login elsewhere (you or a parent in a browser)
    ends ours, and our login would end theirs. So after losing the session, wait an hour before
    logging back in. A message or the daily summary still logs in right away if needed."""
    now = datetime.now()
    if now < datetime.fromisoformat(state.get("next_keepalive", "2000-01-01T00:00")):
        return
    try:
        if attendance.session_alive():
            state.pop("session_lost_at", None)
        elif "session_lost_at" not in state:
            log("Portal session ended (someone else logged in?), logging in again in an hour")
            state["session_lost_at"] = now.isoformat(timespec="seconds")
        elif now - datetime.fromisoformat(state["session_lost_at"]) >= RELOGIN_AFTER:
            log("Logging in to the portal again")
            refresh(state)
            state.pop("session_lost_at", None)
        state["next_keepalive"] = (now + KEEPALIVE_EVERY).isoformat(timespec="seconds")
    except Exception:
        log(traceback.format_exc())
        state["next_keepalive"] = (now + RELOGIN_AFTER).isoformat(timespec="seconds")  # don't hammer it
    save_state(state)


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

    fetched = datetime.fromisoformat(state["fetched_at"])
    checked = fetched.strftime("%H:%M" if fetched.date() == date.today() else "%-d %b, %H:%M")
    lines += [
        "<pre>" + html.escape("\n".join(rows)) + "</pre>",
        f"<i>Skip = classes you can still miss and end the semester at {REQUIRED}%</i>",
        f"<i>Portal updated {pretty_day(state['data']['last_updated'])} · checked {checked}</i>",
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


def daily_due(state):
    now = datetime.now()
    hour, minute = map(int, DAILY_AT.split(":"))
    return (now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            and state.get("last_daily") != date.today().isoformat())


def send_daily(state):
    # Mark it done before trying, so a broken portal isn't retried every minute all evening
    state["last_daily"] = date.today().isoformat()
    save_state(state)
    try:
        refresh(state)
    except Exception as e:
        log(traceback.format_exc())
        send(f"⚠️ Couldn't check attendance for today's summary: {html.escape(str(e))}\nTry /refresh later.")
        return
    diff = changes(state.get("daily_snapshot"), state["data"])
    send(summary(state, diff))
    # Only once it's sent: if Telegram was unreachable, today's changes roll into tomorrow's summary
    state["daily_snapshot"] = state["data"]
    save_state(state)


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

    # Anything else counts as "show my attendance"
    force = command == "/refresh"
    note = None
    if force or not is_fresh(state):
        note = send("⏳ Checking the portal…")
        try:
            refresh(state)
        except Exception as e:
            log(traceback.format_exc())
            tg("editMessageText", chat_id=CHAT_ID, message_id=note["message_id"],
               text=f"⚠️ Couldn't check the portal: {html.escape(str(e))}", parse_mode="HTML")
            return
    text = summary(state)
    if note:
        tg("editMessageText", chat_id=CHAT_ID, message_id=note["message_id"], text=text, parse_mode="HTML")
    else:
        send(text)


def main():
    if not TOKEN:
        sys.exit("Put TELEGRAM_TOKEN=... in .env")
    try:
        datetime.strptime(DAILY_AT, "%H:%M")
    except ValueError:
        sys.exit("DAILY_AT in .env must look like 19:00")
    attendance.load_credentials()  # fail fast on a broken .env

    me = tg("getMe")
    tg("setMyCommands", commands=[
        {"command": "attendance", "description": "Current attendance"},
        {"command": "refresh", "description": "Check the portal right now"},
        {"command": "help", "description": "What this bot does"},
    ])
    log(f"Running as @{me['username']}, daily summary at {DAILY_AT}"
        + ("" if CHAT_ID else " (TELEGRAM_CHAT_ID not set: only logging who writes)"))

    state = load_state()
    while True:
        try:
            if CHAT_ID and daily_due(state):
                send_daily(state)
            keep_session_warm(state)
            updates = tg("getUpdates", offset=state.get("offset", 0), timeout=50, allowed_updates=["message"])
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"Telegram unreachable ({e}), retrying in 15s")  # offline, or laptop just woke up
            time.sleep(15)
            continue
        for update in updates:
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
