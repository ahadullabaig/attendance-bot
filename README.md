# MSRIT Attendance Bot

Check your attendance on the [MSRIT parents portal](https://parents.msrit.edu/newparents/index.php) without logging in by hand. A small Chrome extension logs in from your own browser once a day and sends the pages to a Telegram bot. The bot runs 24/7, answers `/attendance` straight away, and sends a summary every evening with what changed since the day before.

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
| `/attendance` (or any message) | The latest numbers from your laptop. If they're more than a day old, it says so. |
| `/refresh` | Asks your laptop to check the portal now. With Chrome open, the numbers arrive within a few minutes. |
| `/help` | Lists the commands. |

- **Daily summary** at `DAILY_AT` (default 19:00, local time), with a "Since last summary" section listing new presents and absences. It waits for that day's numbers. If they haven't arrived three hours later, it says so once, and the summary follows when they do.
- **Only answers you.** Messages from any chat other than `TELEGRAM_CHAT_ID` are ignored and logged.
- **Never contacts the portal itself.** See [How it works](#how-it-works) for why.

**Chrome extension** (`extension/`)

- Asks the bot every 2 minutes whether a check is due. That traffic goes only to the bot.
- A check is due once a day from 30 minutes before `DAILY_AT` (18:30 by default), and after each `/refresh`. If Chrome isn't open then, the check happens the next time it is.
- Logs in to the portal in a background tab with your details, reads the dashboard and each course page about a second and a half apart, sends them to the bot and closes the tab.
- **Doesn't log you out.** The portal allows one login at a time, so the extension skips a check while you have the portal open in Chrome.
- **Goes easy on the portal.** After a failed check the bot waits an hour and tries once more, then leaves it until the next day. The extension itself never starts automatic checks less than 10 minutes apart, or more than 4 times a day.
- The first check is always one you start and watch. Automatic checks turn on after it succeeds.

**Command line** (`attendance.py`)

```
uv run attendance.py            # table in the terminal
uv run attendance.py --json     # machine-readable
uv run attendance.py --show     # visible browser, e.g. to solve a captcha puzzle
uv run attendance.py --dump     # also save raw pages to debug/
```

The command line logs in from wherever you run it, by default with a headless browser, which is the kind of client the portal's firewall now bans. Prefer the extension. If you do use the command line, add `--show`.

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
NoSQL           100    6/6    2
NLP               –      –    –
Skip = classes you can still miss and end the semester at 85%
Portal updated 28 Sep · checked 18:31
```

Long course names are shortened automatically for the table: `Parallel Programming on GPUs` → `PPG`, `Natural Language Processing Laboratory` → `NLP Lab`. Names that already fit are kept as they are, unless `NICKNAMES` in `bot.py` picks one by hand (`NoSQL Databases` → `NoSQL`).

## How it works

```
 your laptop                                     Fly.io
┌──────────────────────────────┐  GET /due     ┌─────────────────────────┐
│ Chrome + extension/          │──────────────►│ bot.py                  │
│  is a check due? if so,      │               │  decides when to check  │
│  in a background tab:        │  POST /push   │  reads the pages with   │
│  log in, fetch the dashboard │──────────────►│  attendance.py          │──► Telegram
│  and each course page        │               │  math, daily summary    │
└──────────┬───────────────────┘               └─────────────────────────┘
           │ your own connection,
           ▼ once a day
   parents.msrit.edu
```

1. **The bot never contacts the portal.** The portal came back from a week-long outage in late October 2026 with a firewall that bans IPs that look like bots. It banned the bot's Fly.io server within the hour. So the logging in moved to your own browser, on your own connection, at the pace of a person clicking through.
2. **The bot decides, the extension does.** The extension asks `GET /due` every 2 minutes. The bot answers yes once a day and after `/refresh`, and backs off after failures (see [Features](#features)).
3. **The extension logs in like you would.** It opens the login page, fills in your USN and date of birth, waits for the invisible reCAPTCHA, then picks "Father Mobile" and types the last 4 digits. Your real Chrome profile is what the captcha sees, so it rarely has doubts.
4. **Only the bot reads pages.** The extension fetches the dashboard and sends it to `POST /push`. The bot answers with the course pages it needs, the extension fetches those and sends them too. Only `attendance.py`'s parsers know what the portal's pages look like.
5. **Failures come to the chat.** If a check fails, the extension sends the reason and the page it got stuck on to `POST /failed`. The bot saves the page to `DATA_DIR/debug/` and tells you in Telegram.

All three addresses need the `PUSH_TOKEN`, and Fly only serves them over HTTPS.

### Portal quirks (learned the hard way)

These explain most of the design decisions:

- **Every request needs a `Referer` from the portal.** A request without one, such as typing a URL straight into the address bar, logs the session out on the spot. The extension sends one with every page it fetches.
- **One session per user.** Any new login, whether the extension, the laptop CLI or a person in a browser, ends the previous session immediately.
- **The firewall bans bot-like clients by IP.** Since October 2026, banned addresses get `Connection refused` while everyone else gets the portal. A headless browser's User-Agent, or a script's, is the likeliest trigger.
- **The session is tied to the browser.** HTTP requests must reuse the exact User-Agent of the browser that logged in. (The CLI saves it with the cookie.)
- **The session cookie has no expiry date**, so browsers drop it when they close. That's why the portal always asks you to log in again.
- **Every page redirects to `index.php?ksign=…`**, an encrypted version of the real URL. The readable URLs (`task=dashboard`, `task=attendencelist&courseId=…`) still work as long as the Referer is present.
- **The date of birth is sent as `YYYY-MM-DD`**, built from three dropdowns by the page's own `putdate()` function. The day options' values have a trailing space (`'16 '`), so days are picked by their visible label.
- **Empty counts mean zero.** A course with no classes recorded shows `Present[]` and `Absent []`; the parser treats these as 0.
- **An "Attendance rules" popup covers the dashboard** and blocks clicks. Pages are loaded by URL instead of by clicking.

## Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/). The scripts declare their dependencies inline (PEP 723), so `uv run` installs them.
- Google Chrome on the laptop you use every day, for the extension.
- A Telegram account, for the bot.
- The [Fly.io CLI](https://fly.io/docs/flyctl/install/), for running the bot 24/7.

### 1. Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
2. `cp .env.example .env && chmod 600 .env`, and put the token BotFather gives you in `.env` as `TELEGRAM_TOKEN=...`.
3. Send your new bot any message, then find your chat ID:
   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
   ```
4. Put it in `.env` as `TELEGRAM_CHAT_ID=...`.

If `TELEGRAM_CHAT_ID` isn't set, the bot doesn't answer anyone. It logs the chat ID of whoever messages it, which is another way to find yours.

### 2. Push token

The bot and the extension share a secret. Make one and put it in `.env` as `PUSH_TOKEN=...`:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

### 3. Chrome extension

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the `extension` folder.
2. Pin the extension (puzzle-piece icon → pin) and click it to open its settings.
3. Fill in the push token, your USN, date of birth and the last 4 digits of your father's mobile, and click **Save**. They stay in this Chrome profile only.
4. Once the bot is running (next section), close any portal tabs and click **Run now**. The portal opens in a tab in front of you, logs in and closes again. When the settings page says it sent your attendance, automatic checks turn on.

**Recent checks** on the settings page lists the last 20 checks and what happened. After you pull a new version of the extension, click the reload icon on its card in `chrome://extensions`.

### 4. Command line (optional)

Fill in `USN`, `DOB` (`YYYY-MM-DD`) and `FATHER_LAST4` in `.env`. Then `uv run attendance.py --show`, but read the warning under [Features](#features) first.

## Running it

Run the bot in **one place only**. Two copies polling Telegram at the same time conflict with each other.

### Option A: on a laptop

```sh
uv run bot.py
```

The extension expects the bot at `https://attendance-bot-6767.fly.dev`. To use a local bot instead, change `BOT` at the top of `extension/background.js` to `http://localhost:8080`, add `http://localhost/*` to `host_permissions` in `extension/manifest.json`, and reload the extension.

To start the bot automatically when you log in (systemd user service):

```sh
ln -s ~/attendance-bot/attendance-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now attendance-bot
```

### Option B: on Fly.io, 24/7 (current setup)

`fly.toml` describes one shared-cpu-1x machine with 512 MB of RAM, a 1 GB volume at `/data`, region `sin`, and the web server the extension talks to on port 8080 behind HTTPS. Fly no longer accepts new resources in Mumbai (`bom`).

First deploy:

```sh
fly auth login
fly apps create attendance-bot-6767            # app names are global; change it in fly.toml and background.js if taken
fly volumes create bot_data --region sin --size 1 --yes
fly ips allocate-v4 --shared                   # free; gives attendance-bot-6767.fly.dev an address
fly ips allocate-v6
grep -E '^(TELEGRAM_|DAILY_AT|PUSH_TOKEN)' .env | fly secrets import --stage
fly deploy --ha=false --remote-only
```

- `--ha=false` keeps it to a single machine. Two machines would both poll Telegram.
- `--remote-only` builds the image on Fly's builders.
- The image contains only `attendance.py` and `bot.py`, as enforced by `.dockerignore`, and no browser. Secrets come from Fly. The bot doesn't need your portal login; only the extension has it.

Stop any laptop copy afterwards: `systemctl --user disable --now attendance-bot`.

### Automatic deploys

`.github/workflows/deploy.yml` runs the tests on every pull request. When a push to `main` changes the code, `Dockerfile`, `fly.toml` or the workflow itself, it runs the tests and then deploys. Pushes that only touch docs, tests or the extension don't deploy, since that would just restart the bot.

It deploys with a token that can only deploy this app, stored as the repository secret `FLY_API_TOKEN`. To create or replace it:

```sh
fly tokens create deploy -a attendance-bot-6767 -n "GitHub Actions" | gh secret set FLY_API_TOKEN
```

`fly tokens list -a attendance-bot-6767` shows the app's tokens, and `fly tokens revoke <id>` revokes one.

A manual `fly deploy` still works, but it ships your local folder, including uncommitted changes. Merging to `main` keeps GitHub and production the same.

## Operations

```sh
fly logs -a attendance-bot-6767                          # live log
fly deploy --ha=false --remote-only                      # deploy by hand (pushing code to main also deploys)
fly secrets set DAILY_AT=20:30 -a attendance-bot-6767    # change a setting (restarts the bot)
fly scale count 0 -a attendance-bot-6767                 # pause; `count 1` to resume
fly ssh console -a attendance-bot-6767                   # shell on the machine
fly ssh console -a attendance-bot-6767 -C "cat /data/.bot-state.json"     # cache and state
```

What shows up in the log:

| Line | Meaning |
|---|---|
| `Running as @…, daily summary at 19:00, listening for the extension on port 8080` | Started. |
| `Got attendance for 7 courses from the extension` | A check worked. |
| `The extension's portal check failed: …` | A check didn't work; the rest of the line says why. The chat gets the same message. |
| `Ignoring message from chat …` | A stranger messaged the bot. |
| `Telegram unreachable (…), retrying in 15s` | Network blip. |
| `Stopping` | Normal shutdown during a deploy or restart. |

**Cost:** about $3–4 a month (small machine plus a 1 GB volume; the shared IPv4 address is free). Check the Fly dashboard's Billing page for the exact figure.

## Configuration

Set these in `.env` locally, or with `fly secrets set` on Fly. Real environment variables take precedence over `.env`.

| Variable | Needed by | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_TOKEN` | bot | – | From @BotFather |
| `TELEGRAM_CHAT_ID` | bot | – | The only chat the bot answers |
| `PUSH_TOKEN` | bot | – | Shared with the extension; at least 20 characters |
| `DAILY_AT` | – | `19:00` | Daily summary time (`HH:MM`, local time; the container is set to `Asia/Kolkata`). The extension checks the portal 30 minutes before. |
| `PORT` | – | `8080` | The extension's web server; `fly.toml` expects 8080 |
| `DATA_DIR` | – | project folder | Where bot state and debug pages go (`/data` in the container) |
| `USN` | CLI | – | Portal username, e.g. `1MS24AI000` |
| `DOB` | CLI | – | Date of birth, `YYYY-MM-DD` |
| `FATHER_LAST4` | CLI | – | Last 4 digits of the father's mobile number (the verification step) |
| `BROWSER_CHANNEL` | CLI | `chrome` | Playwright browser channel |

The extension keeps its own copy of your portal login, in its settings page.

Tuning constants in the code:

| Constant | File | Value |
|---|---|---|
| `REQUIRED` / `CONDONABLE` | `attendance.py` | 85 / 75 (the portal's own rules) |
| `FETCH_LEAD` | `bot.py` | 30 minutes before `DAILY_AT` |
| `RETRY_AFTER` / `MAX_TRIES` | `bot.py` | 1 hour / 2 automatic checks a day |
| `NOTICE_AFTER` | `bot.py` | 3 hours after `DAILY_AT` |
| `TICK_MINUTES` | `extension/background.js` | 2 |
| `MIN_GAP` / `MAX_RUNS_PER_DAY` | `extension/background.js` | 10 minutes / 4 |
| `PAGE_GAP` | `extension/background.js` | 1.5 seconds between course pages |

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

The extension's settings page (click its icon) lists recent checks and what happened. Failed checks also arrive in the chat.

| Symptom | Cause / fix |
|---|---|
| `No summary yet: your laptop hasn't checked the portal today` | Chrome wasn't open, or automatic checks are off on the settings page. Open Chrome; the summary follows within a few minutes. |
| `The portal showed the login page again after logging in` | The USN or date of birth in the extension's settings is wrong. |
| `The verification page came back` | The last 4 digits in the extension's settings are wrong. |
| `reCAPTCHA didn't pass` | Google wanted a puzzle. Click **Run now**, which shows the tab, and solve it. |
| `The portal didn't load (net::ERR_CONNECTION_REFUSED)` | The portal is down, or it has blocked your connection. If it opens on your phone's mobile data, it's a block: turn off automatic checks and talk to college IT rather than retrying. |
| `isn't the dashboard` / `Couldn't read the attendance page` | The portal's layout probably changed. The page is saved on the bot's machine; fetch it with `fly ssh sftp get /data/debug/<file> -a attendance-bot-6767` and compare it with the parser in `attendance.py`. |
| `The portal is open in a tab` | The extension won't log in while you're using the portal, since that would log you out. Close the tab. |
| The extension does nothing | Check that automatic checks are on in its settings. For details, open `chrome://extensions`, click **service worker** on its card, and look at the console. |
| `The bot didn't accept the token` | The push token in the extension's settings doesn't match `PUSH_TOKEN` on the bot. |
| `409 Conflict` / `Telegram unreachable` repeating in the log | Two copies of the bot are polling Telegram. Stop one. |

## Project layout

```
attendance.py           portal login for the CLI, page reading, math
bot.py                  Telegram bot and the extension's web server: commands, schedule, daily summary
extension/              Chrome extension: logs in from your browser and sends the pages to the bot
tests.py                offline tests: uv run tests.py
test_extension.py       the extension in Chromium against a fake portal: uv run test_extension.py
Dockerfile              Python 3.12, no browser, TZ=Asia/Kolkata
fly.toml                Fly.io machine, volume, web service and restart policy
attendance-bot.service  systemd user unit for running the bot on a laptop
.env.example            configuration template
.github/workflows/      GitHub Actions: tests on pull requests, tests + deploy on main
```

Neither test file reaches the portal or Telegram. `tests.py` writes only to a temporary folder and is safe to run while the bot is up. `test_extension.py` runs the real extension and the bot's web server against a fake portal on your machine, and needs Chromium rather than Google Chrome (see its docstring). The test pages are hand-made, not saved from the portal.

Git-ignored runtime files (in `DATA_DIR`):

```
.env                 credentials and tokens
.bot-state.json      Telegram offset, latest numbers, daily snapshot, check schedule
debug/               pages saved when reading fails, or with --dump
.session.json        CLI only: portal session cookie + User-Agent
.browser-profile/    CLI only: Chromium profile (keeps Google's cookies, which helps the captcha)
```

## Limitations

- **Needs your laptop:** checks only happen while Chrome is open. A day the laptop stays shut gets a notice instead of a summary, and the numbers catch up the next time Chrome opens.
- **Data freshness:** the portal updates attendance about once a day ("Last Updated On"), so checking more often shows nothing new.
- **Screen scraping:** the bot reads the portal's pages, so a portal redesign will break the extension's login or the parsing until the code is updated.
- **One session:** the extension's daily login logs out anyone using the portal at that moment, such as a parent. It does skip while you have the portal open in Chrome.
- **Personal use:** one login a day from your own browser. Keep it that way.
