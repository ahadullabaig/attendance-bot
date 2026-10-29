# MSRIT Attendance Bot

Check your attendance on the [MSRIT parents portal](https://parents.msrit.edu/newparents/index.php) without logging in by hand. A Telegram bot runs 24/7, answers `/attendance` in about two seconds, and sends a summary every evening with what changed since the day before.

It also does the math the portal doesn't: how many classes you can still skip in each course, and how many in a row you need to attend to get back above 85%.

- [Features](#features)
- [How it works](#how-it-works)
- [Setup](#setup)
- [Running it](#running-it)
- [Operations](#operations)
- [Configuration](#configuration)
- [The math](#the-math)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Limitations](#limitations)

## Features

**Telegram bot** (`bot.py`)

| Command | What it does |
|---|---|
| `/attendance` (or any message) | Current summary. Served from a cache if the last check is under 3 hours old, since the portal only updates once a day. If the portal is down, you get the last successful check with a warning. |
| `/refresh` | Checks the portal right now. |
| `/help` | Lists the commands. |

- **Daily summary** at `DAILY_AT` (default 19:00, local time), with a "Since last summary" section listing new presents and absences. If the portal is down, it says so once and tries again every hour until midnight.
- **Only answers you.** Messages from any chat other than `TELEGRAM_CHAT_ID` are ignored and logged.
- **Keeps the portal session warm**, so answers take seconds instead of a minute-long browser login.
- **Doesn't fight you for the session.** The portal allows one login at a time. If you or a parent log in by hand, the bot backs off for an hour instead of kicking you out.

**Command line** (`attendance.py`)

```
uv run attendance.py            # table in the terminal
uv run attendance.py --json     # machine-readable
uv run attendance.py --show     # visible browser, e.g. to solve a captcha puzzle
uv run attendance.py --dump     # also save raw pages to debug/
```

### Example message

The first line is what the phone notification shows, so it carries the verdict. (The numbers here are made up.)

```
📊 Attendance — 2 courses below 85%

🆕 Since last summary
• Big Data Computing: +2 present
• Parallel Programming on GPUs: absent 22-09-2026

⚠️ Parallel Programming on GPUs is at 75% — attend the next 8 classes to get back to 85%
⚠️ Computer Organization and Architecture is at 83% — attend the next 2 classes to get back to 85%

Course            %   Came Skip
PPG              75   9/12    3
COA              83  10/12    4
BDC              94  17/18    6
OS               92  12/13    5
PP Lab          100    6/6    2
NoSQL Databases 100    6/6    2
NLP               –      –    –
Skip = classes you can still miss and end the semester at 85%
Portal updated 28 Sep · checked 19:00
```

Long course names are shortened automatically for the table: `Parallel Programming on GPUs` → `PPG`, `Natural Language Processing Laboratory` → `NLP Lab`. Names that already fit are kept as they are.

## How it works

```
                    only when the session is gone
                ┌──────────────────────────────────────┐
                │ Chromium (Playwright)                │
                │  USN + DOB → invisible reCAPTCHA     │
                │  → father's mobile, last 4 digits    │
                └──────────────┬───────────────────────┘
                               │ cookies + User-Agent
                               ▼
 Telegram ◄── bot.py ◄── plain HTTP (requests) ──► parents.msrit.edu
              │  dashboard: course list + %
              │  one page per course: present / absent / still to go
              ▼
         .bot-state.json  (cache, daily snapshot, Telegram offset)
```

1. **Log in with a real browser, but only when needed.** The login page has an invisible Google reCAPTCHA that runs when the page loads, so logging in needs a real browser. Headless Chromium passes it, both on a laptop and on a Fly.io server.
2. **Read everything else over plain HTTP.** After login, the session cookie and the browser's User-Agent are saved to `.session.json`. The dashboard and the per-course attendance pages are then fetched with `requests`. On a small server this takes about 2 seconds, against about 75 for a browser login.
3. **Keep the session alive.** Every 10 minutes the bot loads the dashboard with the saved session. This keeps the session from expiring and shows whether it's still valid.
4. **Back off when someone else logs in.** If the session is gone, the bot waits an hour before logging in again in the background. A message or the daily summary still logs in straight away if it has to.

### Portal quirks (learned the hard way)

These explain most of the design decisions:

- **Every request needs a `Referer` from the portal.** A request without one, such as typing a URL straight into the address bar, logs the session out on the spot. All requests send `Referer: <portal page>`.
- **One session per user.** Any new login, whether the bot, the laptop CLI or a person in a browser, ends the previous session immediately.
- **The session is tied to the browser.** HTTP requests reuse the exact User-Agent of the browser that logged in.
- **The session cookie has no expiry date**, so browsers drop it when they close. That's why the portal always asks you to log in again. The bot stores the cookie itself.
- **Every page redirects to `index.php?ksign=…`**, an encrypted version of the real URL. The readable URLs (`task=dashboard`, `task=attendencelist&courseId=…`) still work as long as the Referer is present.
- **The date of birth is sent as `YYYY-MM-DD`**, built from three dropdowns by the page's own `putdate()` function. The day options' values have a trailing space (`'16 '`), so the script selects days by their visible label.
- **Empty counts mean zero.** A course with no classes recorded shows `Present[]` and `Absent []`; the parser treats these as 0.
- **An "Attendance rules" popup covers the dashboard** and blocks clicks. The code navigates by URL instead of clicking.

## Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/). The scripts declare their dependencies inline (PEP 723), so `uv run` installs them.
- Google Chrome, for running locally. The container uses Playwright's own Chromium instead.
- A Telegram account, for the bot.
- The [Fly.io CLI](https://fly.io/docs/flyctl/install/), for running it 24/7.

### 1. Credentials

```sh
cp .env.example .env
chmod 600 .env
```

Fill in `USN`, `DOB` (`YYYY-MM-DD`) and `FATHER_LAST4`. Check that it works:

```sh
uv run attendance.py
```

### 2. Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
2. Put the token it gives you in `.env` as `TELEGRAM_TOKEN=...`.
3. Send your new bot any message, then find your chat ID:
   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
   ```
4. Put it in `.env` as `TELEGRAM_CHAT_ID=...`.

If `TELEGRAM_CHAT_ID` isn't set, the bot doesn't answer anyone. It logs the chat ID of whoever messages it, which is another way to find yours.

## Running it

Run the bot in **one place only**. Two copies polling Telegram at the same time conflict with each other.

### Option A: on a laptop

```sh
uv run bot.py
```

To start it automatically when you log in (systemd user service):

```sh
ln -s ~/attendance-bot/attendance-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now attendance-bot
```

It only runs while the laptop is awake. Telegram holds messages for up to 24 hours, so the bot catches up when the laptop wakes.

### Option B: on Fly.io, 24/7 (current setup)

`fly.toml` describes one shared-cpu-1x machine with 512 MB of RAM and 512 MB of swap, a 1 GB volume at `/data`, and region `sin`. Fly no longer accepts new resources in Mumbai (`bom`), and Singapore reaches the portal in about 0.25 s.

First deploy:

```sh
fly auth login
fly apps create attendance-bot-6767            # app names are global; change it in fly.toml if taken
fly volumes create bot_data --region sin --size 1 --yes
grep -E '^[A-Z0-9_]+=' .env | fly secrets import --stage
fly deploy --ha=false --remote-only
```

- `--ha=false` keeps it to a single machine. Two machines would both poll Telegram.
- `--remote-only` builds the image on Fly's builders.
- The image contains only `attendance.py` and `bot.py`, as enforced by `.dockerignore`. Credentials come from Fly secrets.

Stop any laptop copy afterwards: `systemctl --user disable --now attendance-bot`.

## Operations

```sh
fly logs -a attendance-bot-6767                          # live log
fly deploy --ha=false --remote-only                      # ship code changes
fly secrets set DAILY_AT=20:30 -a attendance-bot-6767    # change a setting (restarts the bot)
fly scale count 0 -a attendance-bot-6767                 # pause; `count 1` to resume
fly scale memory 1024 -a attendance-bot-6767             # if Chromium ever runs out of memory
fly ssh console -a attendance-bot-6767                   # shell on the machine
```

What shows up in the log:

| Line | Meaning |
|---|---|
| `Running as @…, daily summary at 19:00` | Started. |
| `Portal session ended (someone else logged in?), logging in again in an hour` | Someone logged in elsewhere; the bot is backing off. |
| `Logging in to the portal again` | The hour is up; logging in in the background. |
| `Portal check failed: …` | A check didn't work; the rest of the line says why, e.g. the portal isn't responding. |
| `Ignoring message from chat …` | A stranger messaged the bot. |
| `Telegram unreachable (…), retrying in 15s` | Network blip. |
| `Stopping` | Normal shutdown during a deploy or restart. |

Useful one-offs on the machine:

```sh
fly ssh console -a attendance-bot-6767 -C "python attendance.py"          # full fetch, prints the table
fly ssh console -a attendance-bot-6767 -C "cat /data/.bot-state.json"     # cache and state
```

**Cost:** about $3–4 a month (small machine plus a 1 GB volume). Check the Fly dashboard's Billing page for the exact figure.

## Configuration

Set these in `.env` locally, or with `fly secrets set` on Fly. Real environment variables take precedence over `.env`.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `USN` | yes | – | Portal username, e.g. `1MS24AI000` |
| `DOB` | yes | – | Date of birth, `YYYY-MM-DD` |
| `FATHER_LAST4` | yes | – | Last 4 digits of the father's mobile number (the verification step) |
| `TELEGRAM_TOKEN` | bot | – | From @BotFather |
| `TELEGRAM_CHAT_ID` | bot | – | The only chat the bot answers |
| `DAILY_AT` | no | `19:00` | Daily summary time (`HH:MM`, local time; the container is set to `Asia/Kolkata`) |
| `DATA_DIR` | no | project folder | Where the session, browser profile, bot state and debug pages go (`/data` in the container) |
| `BROWSER_CHANNEL` | no | `chrome` | Playwright browser channel (`chromium` in the container) |

Tuning constants in the code:

| Constant | File | Value |
|---|---|---|
| `REQUIRED` / `CONDONABLE` | `attendance.py` | 85 / 75 (the portal's own rules) |
| `CACHE_FOR` | `bot.py` | 3 hours |
| `KEEPALIVE_EVERY` | `bot.py` | 10 minutes |
| `RELOGIN_AFTER` | `bot.py` | 1 hour, both after losing the session and after a failed login |

## The math

Each course's attendance page gives **P** (present), **A** (absent) and **R** (still to go). Classes held so far: `H = P + A`. Total for the semester: `T = H + R`.

**Skip budget:** how many of the remaining classes you can miss and still end the semester at 85%.

```
needed = ceil(0.85 × T)
skip   = R − max(0, needed − P)
```

If `skip` is negative, 85% is out of reach even with full attendance. In that case the bot shows the best percentage you can still reach.

**Catch-up:** the smallest number of classes in a row that brings you back to 85% right now. This matters because the CIEs also require 85%.

```
(P + x) / (H + x) ≥ 0.85   ⇒   x = ceil((85·H − 100·P) / 15)
```

Example: 9 present out of 12 held (75%) → `ceil((1020 − 900) / 15) = 8` classes, and 17/20 is exactly 85%.

Everything uses integer arithmetic, so there are no floating-point rounding errors at the boundary.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `reCAPTCHA didn't pass` | Google wanted a puzzle. Locally, run `uv run attendance.py --show` and solve it. On a server there's no screen to solve it on, so it would need a code change. So far headless logins have always passed. |
| `parents.msrit.edu isn't responding` | The portal is down, sometimes for days. Nothing to fix: messages show the last successful check, and the bot logs back in by itself once the portal returns. |
| `The portal answered with HTTP … but no login form` | The portal is up but served something else: a maintenance notice, an error or block page, or a redesigned login. Fetch the saved screenshot with `fly ssh sftp get /data/debug/login-page.png -a attendance-bot-6767` and compare it with `login()` in `attendance.py`. |
| `Verification step failed` / `couldn't find the attendance table` | The portal's layout probably changed. The page is saved to `debug/` (`/data/debug` on Fly); compare it with the parser in `attendance.py`. |
| Bot replies slowly after you used the portal yourself | Expected: your login ended the bot's session, so it had to log in again (about 75 s on Fly). |
| The laptop CLI keeps logging in | Something else, usually the bot, holds the only session. That's expected, not a bug. |
| `409 Conflict` / `Telegram unreachable` repeating in the log | Two copies of the bot are polling Telegram. Stop one. |
| Machine restarts with out-of-memory errors | `fly scale memory 1024`. A login peaks at about 460 MB. |
| Daily summary didn't arrive | It's sent once per day, at or after `DAILY_AT`. If the portal or Telegram is down, it tries again every hour until midnight, and for the portal it sends one short notice. |

## Project layout

```
attendance.py           portal login, page reading, math, CLI
bot.py                  Telegram bot: commands, daily summary, cache, session keep-alive
tests.py                offline tests: uv run tests.py
Dockerfile              Python 3.12 + Playwright Chromium, TZ=Asia/Kolkata
fly.toml                Fly.io machine, volume and restart policy
attendance-bot.service  systemd user unit for running the bot on a laptop
.env.example            configuration template
```

The tests never log in to the portal or message Telegram, and they write only to a temporary folder, so they're safe to run while the bot is up. Their HTML pages are hand-made, not saved from the portal.

Git-ignored runtime files (in `DATA_DIR`):

```
.env                 credentials and bot token
.session.json        portal session cookie + User-Agent
.browser-profile/    Chromium profile (keeps Google's cookies, which helps the captcha)
.bot-state.json      Telegram offset, cached data, daily snapshot, keep-alive timers
debug/               pages saved when parsing fails, or with --dump
```

## Limitations

- **Data freshness:** the portal updates attendance about once a day ("Last Updated On"), so checking more often shows nothing new.
- **Screen scraping:** the bot reads the portal's pages, so a portal redesign will break parsing until the code is updated.
- **One session:** the bot and anyone using the portal by hand take turns. The bot is designed to be the one that yields.
- **Personal use:** keep the check rate low. The keep-alive is one page load every 10 minutes, plus the pages each check reads.
