# MSRIT Attendance Bot

Keep an eye on your attendance on the [MSRIT parents portal](https://parents.msrit.edu/newparents/index.php) from Telegram. Once a day the bot reminds you to log in to the portal. When you do, a small Chrome extension reads your attendance pages from that session and sends them to the bot. The bot runs 24/7, answers `/attendance` straight away, and sends a summary every evening with what changed since the day before.

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
| `/attendance` (or any message) | The latest numbers. If they're more than a day old, it says so. |
| `/refresh` | Asks for fresh numbers the next time you're logged in to the portal in Chrome. Already logged in? Click the extension's icon on the portal page instead. |
| `/help` | Lists the commands. |

- **Daily reminder** 30 minutes before `DAILY_AT` (18:30 by default) to log in to the portal, unless that day's numbers are already in.
- **Summary on demand:** a click on the extension's icon, or `/refresh`, gets you the summary as soon as the read works. Automatic reads wait for the daily summary.
- **Daily summary** at `DAILY_AT` (default 19:00, local time), with a "Since last summary" section listing new presents and absences. It waits for that day's numbers. If they haven't arrived three hours later, it says so once, and the summary follows when they do.
- **Only answers you.** Messages from any chat other than `TELEGRAM_CHAT_ID` are ignored and logged.
- **Never contacts the portal itself.** See [How it works](#how-it-works) for why.

**Chrome extension** (`extension/`)

- **Never logs in.** You log in to the portal yourself, in a normal tab: USN, date of birth, security code, verification. The extension doesn't fill in, click or submit anything on the portal, doesn't open or close tabs, and doesn't keep your portal login.
- **Reads once you're in.** When a page of this term's portal (`/newparents/`, past the login) finishes loading, it asks the bot whether a read is due. If so, it fetches the dashboard and each course page from your session, about a second and a half apart, and sends them to the bot. The icon shows … while it reads and ✓ once it's sent, so stay on the page for those few seconds.
- A read is due once a day from 30 minutes before `DAILY_AT` (18:30 by default), and after each `/refresh`.
- **Click the icon to send now.** On a portal page, clicking the extension's icon reads straight away, due or not, and the bot sends you the summary as soon as the read works. Anywhere else it opens the settings.
- **Stands down after a failure.** If an automatic read fails, automatic reads pause, so a broken portal page doesn't trigger a read on every page you open. The next read that works, such as a click on the icon, ends the pause. Leaving the page mid-read isn't a failure; the next portal page tries again.
- **One read at a time.** It waits a second for a page that redirects by script, and two portal tabs loading together start one read, not two.

**Command line** (`attendance.py`)

```
uv run attendance.py            # table in the terminal
uv run attendance.py --json     # machine-readable
uv run attendance.py --show     # visible browser, to type the security code or solve a captcha puzzle
uv run attendance.py --dump     # also save raw pages to debug/
```

The command line logs in by script, which the portal has refused since 14 November 2026 ("Login not allowed", see [Portal quirks](#portal-quirks-learned-the-hard-way)), and by default with a headless browser, the kind of client its firewall bans. It's kept for reference; use the extension.

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
 your laptop                                       Fly.io
┌────────────────────────────────┐  GET /due     ┌─────────────────────────┐
│ Chrome: you log in to the      │──────────────►│ bot.py                  │
│ portal. extension/ asks: is a  │               │  decides when to read,  │
│ read due? if so, from your     │  POST /push   │  reminds you            │
│ session: fetch the dashboard   │──────────────►│  reads the pages with   │──► Telegram
│ and each course page           │               │  attendance.py, math    │
└──────────┬─────────────────────┘               └─────────────────────────┘
           │ your own connection,
           ▼ once a day
   parents.msrit.edu
```

1. **The bot never contacts the portal.** The portal came back from a week-long outage in late October 2026 with a firewall that bans IPs that look like bots. It banned the bot's Fly.io server within the hour. So the portal traffic moved to your own browser, on your own connection, at the pace of a person clicking through.
2. **You log in; nothing else does.** From 31 October the extension logged in by itself. Since 14 November the portal refuses logins filled in by an extension, so logging in is yours again, and the bot reminds you when a read is due.
3. **The bot decides, the extension reads.** When you're logged in and on a portal page, the extension asks `GET /due`. The bot answers yes once a day and after `/refresh`.
4. **Only the bot reads pages.** The extension fetches the dashboard and sends it to `POST /push`. The bot answers with the course pages it needs, the extension fetches those and sends them too. Only `attendance.py`'s parsers know what the portal's pages look like.
5. **Failures come to the chat.** If a read fails, the extension sends the reason (and nothing from the page you're on) to `POST /failed`, and the bot tells you in Telegram. A dashboard the bot can't read is saved to `DATA_DIR/debug/`.

`/due` still answers the fields extension versions before 2.0 act on (`fetch`, always false, and no `login_url`), so an install that hasn't been updated can't log in by itself.

All three addresses need the `PUSH_TOKEN`, and Fly only serves them over HTTPS.

### Portal quirks (learned the hard way)

These explain most of the design decisions:

- **Every request needs a `Referer` from the portal.** A request without one, such as typing a URL straight into the address bar, logs the session out on the spot. The extension sends one with every page it fetches.
- **One session per user.** Any new login, whether yours, a parent's or the laptop CLI's, ends the previous session immediately.
- **Logins done by a script are refused (since 14 November 2026).** A login where an extension fills in the form gets a page titled "Login not allowed": "This portal cannot be used from third-party apps." The same login typed by hand in the same Chrome works.
- **The firewall bans bot-like clients by IP.** Since October 2026, banned addresses get `Connection refused` while everyone else gets the portal. A headless browser's User-Agent, or a script's, is the likeliest trigger.
- **The session is tied to the browser.** HTTP requests must reuse the exact User-Agent of the browser that logged in. (The CLI saves it with the cookie.)
- **The session cookie has no expiry date**, so browsers drop it when they close. That's why the portal always asks you to log in again.
- **Every page redirects to `index.php?ksign=…`**, an encrypted version of the real URL. The readable URLs (`task=dashboard`, `task=attendencelist&courseId=…`) still work as long as the Referer is present.
- **A typed security code (since 15 November 2026).** Under the date of birth, the login form shows an image of 5 characters with a box to type them into, as well as the invisible reCAPTCHA.
- **The date of birth is sent as `YYYY-MM-DD`**, built from three dropdowns by the page's own `putdate()` function. The day options' values have a trailing space (`'16 '`). (This mattered when the extension logged in.)
- **Empty counts mean zero.** A course with no classes recorded shows `Present[]` and `Absent []`; the parser treats these as 0.
- **An "Attendance rules" popup covers the dashboard** and blocks clicks. Pages are loaded by URL instead of by clicking.
- **The site has several portals.** `parents.msrit.edu` opens on a term chooser (`/webfiles/`) that links to this term's portal (`/newparents/`) and others (`/newparentseven/`, …). The extension only reads `/newparents/`.

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
3. Fill in the push token and click **Save**. That's all it needs: it never logs in, so it has no use for your portal login.
4. Once the bot is running (next section), log in to the portal yourself and, on the first page after the verification step, click the extension's icon. It shows ✓ when it has sent your attendance.

**Recent reads** on the settings page lists the last 20 reads and what happened. After you pull a new version of the extension, click the reload icon on its card in `chrome://extensions`. Use it in one Chrome only: each install reads by itself.

### 4. Command line (optional)

Fill in `USN`, `DOB` (`YYYY-MM-DD`) and `FATHER_LAST4` in `.env`. The portal refuses the command line's scripted login, though; see the note under [Features](#features).

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
- The image contains only `attendance.py` and `bot.py`, as enforced by `.dockerignore`, and no browser. Secrets come from Fly. Neither the bot nor the extension has your portal login.

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
| `Got attendance for 7 courses from the extension` | A read worked. |
| `The extension's portal read failed: …` | A read didn't work; the rest of the line says why. The chat gets the same message. |
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
| `DAILY_AT` | – | `19:00` | Daily summary time (`HH:MM`, local time; the container is set to `Asia/Kolkata`). The day's read falls due, with a reminder, 30 minutes before. |
| `PORT` | – | `8080` | The extension's web server; `fly.toml` expects 8080 |
| `DATA_DIR` | – | project folder | Where bot state and debug pages go (`/data` in the container) |
| `USN` | CLI | – | Portal username, e.g. `1MS24AI000` |
| `DOB` | CLI | – | Date of birth, `YYYY-MM-DD` |
| `FATHER_LAST4` | CLI | – | Last 4 digits of the father's mobile number (the verification step) |
| `BROWSER_CHANNEL` | CLI | `chrome` | Playwright browser channel |

Tuning constants in the code:

| Constant | File | Value |
|---|---|---|
| `REQUIRED` / `CONDONABLE` | `attendance.py` | 85 / 75 (the portal's own rules) |
| `FETCH_LEAD` | `bot.py` | 30 minutes before `DAILY_AT` (the read falls due and the reminder goes out) |
| `RETRY_AFTER` / `MAX_TRIES` | `bot.py` | after a failed read, 1 hour before another counts as due / 2 a day |
| `NOTICE_AFTER` | `bot.py` | 3 hours after `DAILY_AT` |
| `PORTAL` | `extension/background.js` | the only portal it reads, `https://parents.msrit.edu/newparents/` |
| `RUN_TIMEOUT` | `extension/background.js` | 3 minutes per read |
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

The extension's settings page lists recent reads and what happened. Failed reads also arrive in the chat.

| Symptom | Cause / fix |
|---|---|
| `No summary yet: no numbers have come in since 18:30` | You haven't logged in to the portal since the read fell due, or automatic reads are off or paused in the extension's settings. Log in; the summary follows within seconds. |
| `Login not allowed: This portal cannot be used from third-party apps` | The portal's answer to a login done by a script, such as an extension before 2.0. Update or remove that install, and log in by hand. |
| `isn't the dashboard` / `Couldn't read the attendance page` | Your session had ended, or the portal's layout changed. Automatic reads are paused. Log in again and click the icon (a read that works ends the pause); if it fails again, the page is saved on the bot's machine: fetch it with `fly ssh sftp get /data/debug/<file> -a attendance-bot-6767` and compare it with the parser in `attendance.py`. |
| `Stopped: the tab left the page before the read finished` | You clicked on before the ✓. Nothing's wrong; the next portal page reads again. |
| `Log in to the portal first` | You clicked the icon on the login or verification page. |
| `The portal didn't load (net::ERR_CONNECTION_REFUSED)` in Chrome | The portal is down, or it has blocked your connection. If it opens on your phone's mobile data, it's a block: talk to college IT. |
| The extension does nothing | Check its settings: automatic reads must be on and not paused. Check that you're past the verification step (the address has `ksign=` in it). Clicking the icon on the portal page reads regardless. For details, open `chrome://extensions`, click **service worker** on its card, and look at the console. |
| `The bot didn't accept the token` | The push token in the extension's settings doesn't match `PUSH_TOKEN` on the bot. |
| `409 Conflict` / `Telegram unreachable` repeating in the log | Two copies of the bot are polling Telegram. Stop one. |

## Project layout

```
attendance.py           page reading and math (plus the CLI's scripted login, which the portal now refuses)
bot.py                  Telegram bot and the extension's web server: commands, schedule, daily summary
extension/              Chrome extension: reads the pages from your logged-in session and sends them to the bot
tests.py                offline tests: uv run tests.py
test_extension.py       the extension in Chromium against a fake portal: uv run test_extension.py
Dockerfile              Python 3.12, no browser, TZ=Asia/Kolkata
fly.toml                Fly.io machine, volume, web service and restart policy
attendance-bot.service  systemd user unit for running the bot on a laptop
.env.example            configuration template
.github/workflows/      GitHub Actions: tests on pull requests, tests + deploy on main
```

Neither test file reaches the portal or Telegram. `tests.py` writes only to a temporary folder and is safe to run while the bot is up. `test_extension.py` runs the real extension and the bot's web server against a fake portal on your machine, logging in the way you do, and needs Chromium rather than Google Chrome (see its docstring). The test pages are hand-made, not saved from the portal.

Git-ignored runtime files (in `DATA_DIR`):

```
.env                 credentials and tokens
.bot-state.json      Telegram offset, latest numbers, daily snapshot, read schedule
debug/               pages saved when reading fails, or with --dump
.session.json        CLI only: portal session cookie + User-Agent
.browser-profile/    CLI only: Chromium profile (keeps Google's cookies, which helps the captcha)
```

## Limitations

- **Needs you every day:** the numbers only come in when you log in to the portal in Chrome. A day you don't gets a notice instead of a summary.
- **Data freshness:** the portal updates attendance about once a day ("Last Updated On"), so reading more often shows nothing new.
- **Screen scraping:** the bot reads the portal's pages, so a portal redesign breaks the parsing until the code is updated.
- **The portal doesn't want third-party tools.** It refuses scripted logins outright. The extension only reads pages from a session you opened, but whether the portal tolerates that is up to the portal, and it could start refusing it too. If a read ever logs you out or gets refused, stop using the extension.
- **Personal use:** one read a day, from your own browser and session. Keep it that way.
