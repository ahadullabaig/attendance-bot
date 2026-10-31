#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""Offline tests for attendance.py and bot.py. Run with: uv run tests.py

These never reach the portal or Telegram: a portal login from here risks a firewall ban, and a
second Telegram poller would conflict with the running bot. setUpModule turns any such call into a
test error and points every file the code writes at a temporary folder. ServerTest runs the bot's
web server on a random port on 127.0.0.1. The pages below are hand-made from the
parsers' selectors, not saved from the portal, so they hold no personal data."""

import ast
import html
import itertools
import json
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

import attendance
import bot
from attendance import REQUIRED, catch_up, parse_dashboard, parse_detail, skip_budget

HERE = Path(__file__).resolve().parent

# Only the code, name and attendance columns are read; the two in between are placeholders
DASHBOARD = """<html><body>
<div class="cn-last-update">Last Updated On : 28/10/2026</div>
<table>
  <caption>Attendance Status</caption>
  <thead><tr><th>Code</th><th>Course</th><th></th><th></th><th>Attendance</th></tr></thead>
  <tbody>
    <tr><td>CS501</td><td>Operating Systems</td><td></td><td></td>
        <td><a href="index.php?option=com_studentdashboard&amp;task=attendencelist&amp;courseId=CS501">75%</a></td></tr>
    <tr><td>CS502</td><td>Compiler Design</td><td></td><td></td><td>-</td></tr>
    <tr><td colspan="5">a row without course cells</td></tr>
  </tbody>
</table>
</body></html>"""

DETAIL = """<html><body>
<div class="cn-legend">
  <span class="cn-attend">Present[9]</span>
  <span class="cn-absent">Absent [3]</span>
  <span class="cn-still">Still to go[30]</span>
</div>
<table class="cn-attend-list2">
  <thead><tr><th>No.</th><th>Date</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>02-09-2026</td></tr>
    <tr><td>2</td><td>16-09-2026</td></tr>
    <tr><td>3</td><td>22-09-2026</td></tr>
  </tbody>
</table>
</body></html>"""

EMPTY_DETAIL = """<div class="cn-legend"><span class="cn-attend">Present[]</span>
<span class="cn-absent">Absent []</span><span class="cn-still">Still to go[40]</span></div>"""


def course(code, name, present, absent, remaining, absent_dates=()):
    held = present + absent
    return {"code": code, "name": name, "attendance": 100 * present / held if held else 0.0,
            "detail_url": None, "present": present, "absent": absent, "remaining": remaining,
            "absent_dates": list(absent_dates), "can_skip": skip_budget(present, absent, remaining)}


def snapshot(*courses):
    return {"last_updated": "28/10/2026", "courses": list(courses)}


class Forbidden(BaseException):
    """A BaseException, so the bot's own `except Exception` handlers can't swallow it."""


def forbidden(*args, **kwargs):
    raise Forbidden("tests must not reach the portal or Telegram")


_tmp = tempfile.TemporaryDirectory()


def setUpModule():
    tmp = Path(_tmp.name)
    for target, name, value in [
        (bot, "tg", forbidden),                    # the only way to Telegram
        (attendance, "browser_login", forbidden),  # the only two ways to the portal
        (attendance, "http_session", forbidden),
        (bot, "STATE_FILE", tmp / ".bot-state.json"),
        (attendance, "SESSION_FILE", tmp / ".session.json"),
        (attendance, "DEBUG_DIR", tmp / "debug"),
    ]:
        mock.patch.object(target, name, value).start()


def tearDownModule():
    mock.patch.stopall()
    _tmp.cleanup()


class MathTest(unittest.TestCase):
    def test_skip_budget_matches_brute_force(self):
        def expected(p, a, r):
            total = p + a + r
            needed = next(n for n in range(total + 1) if 100 * n >= REQUIRED * total)
            return min(r, p + r - needed)  # negative: short by that many even attending everything

        wrong = [(p, a, r) for p, a, r in itertools.product(range(40), range(20), range(30))
                 if p + a + r and skip_budget(p, a, r) != expected(p, a, r)]
        self.assertEqual(wrong, [])

    def test_catch_up_matches_brute_force(self):
        def expected(p, held):
            x = 0
            while 100 * (p + x) < REQUIRED * (held + x):
                x += 1
            return x

        wrong = [(p, a) for p, a in itertools.product(range(60), range(30))
                 if p + a and catch_up(p, p + a) != expected(p, p + a)]
        self.assertEqual(wrong, [])

    def test_readme_example(self):
        self.assertEqual(catch_up(9, 12), 8)  # 17 of 20 is exactly 85%

    def test_reachable_target_never_needs_more_classes_than_are_left(self):
        # The summary shows both numbers side by side, so they must never contradict each other
        wrong = [(p, a, r) for p, a, r in itertools.product(range(40), range(20), range(30))
                 if p + a and skip_budget(p, a, r) >= 0 and catch_up(p, p + a) > r]
        self.assertEqual(wrong, [])


class ParseTest(unittest.TestCase):
    def test_dashboard(self):
        data = parse_dashboard(DASHBOARD)
        self.assertEqual(data["last_updated"], "28/10/2026")
        self.assertEqual(data["courses"], [
            {"code": "CS501", "name": "Operating Systems", "attendance": 75.0,
             "detail_url": attendance.BASE + "index.php?option=com_studentdashboard&task=attendencelist&courseId=CS501"},
            {"code": "CS502", "name": "Compiler Design", "attendance": None, "detail_url": None},
        ])

    def test_login_page_is_not_a_dashboard(self):
        self.assertIsNone(parse_dashboard("<html><form id='login'></form></html>"))

    def test_detail(self):
        self.assertEqual(parse_detail(DETAIL), {
            "present": 9, "absent": 3, "remaining": 30,
            "absent_dates": ["02-09-2026", "16-09-2026", "22-09-2026"]})

    def test_empty_counts_mean_zero(self):
        self.assertEqual(parse_detail(EMPTY_DETAIL),
                         {"present": 0, "absent": 0, "remaining": 40, "absent_dates": []})

    def test_detail_without_legend(self):
        self.assertIsNone(parse_detail("<html><body>Session expired</body></html>"))


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.http = mock.Mock()

    def respond(self, *pages):
        self.http.get.side_effect = [mock.Mock(text=text, url=url) for text, url in pages]

    def test_saved_session_reads_everything_over_http(self):
        self.respond((DASHBOARD, attendance.DASHBOARD_URL), (DETAIL, "detail"))
        with (mock.patch.object(attendance, "load_session", return_value={"user_agent": "UA", "cookies": []}),
              mock.patch.object(attendance, "http_session", return_value=self.http),
              mock.patch.object(attendance, "save_session") as save_session):
            data = attendance.fetch_attendance()
        first, second = data["courses"]
        self.assertEqual((first["present"], first["absent"], first["remaining"], first["can_skip"]), (9, 3, 30, 3))
        self.assertNotIn("present", second)  # no link to a detail page
        for call in self.http.get.call_args_list:
            self.assertIn("Referer", call.kwargs["headers"])  # without one the portal logs us out
        save_session.assert_called_once_with(self.http)

    def test_new_login_is_saved_before_reading_course_pages(self):
        self.respond(("<html>not an attendance page</html>", "detail"))
        with (mock.patch.object(attendance, "load_session", return_value=None),
              mock.patch.object(attendance, "browser_login",
                                return_value=(DASHBOARD, attendance.DASHBOARD_URL, "UA", [])),
              mock.patch.object(attendance, "http_session", return_value=self.http),
              mock.patch.object(attendance, "save_session") as save_session):
            with self.assertRaises(attendance.PortalError):
                attendance.fetch_attendance()
        save_session.assert_called_once_with(self.http)

    def test_unreachable_portal_says_so(self):
        self.http.get.side_effect = requests.ConnectTimeout("timed out")
        with (mock.patch.object(attendance, "load_session", return_value={"user_agent": "UA", "cookies": []}),
              mock.patch.object(attendance, "http_session", return_value=self.http)):
            with self.assertRaisesRegex(attendance.PortalError, "isn't responding"):
                attendance.fetch_attendance()


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.page = mock.Mock()

    def login(self):
        attendance.login(self.page, "1MS00XX000", "2000-01-01", "0000", captcha_timeout=1)

    def test_unreachable_portal_says_so(self):
        self.page.goto.side_effect = attendance.PWError("net::ERR_CONNECTION_REFUSED")
        with self.assertRaisesRegex(attendance.PortalError, "isn't responding"):
            self.login()

    def test_page_without_login_form_is_saved(self):
        # So the next time the portal serves something odd, there's a page to look at
        self.page.goto.return_value = mock.Mock(status=503)
        self.page.locator.return_value.wait_for.side_effect = attendance.PWTimeout("Timeout 10000ms exceeded")
        with (mock.patch.object(attendance, "dump", return_value="debug/login-page.html") as dump,
              self.assertRaisesRegex(attendance.PortalError,
                                     r"HTTP 503 but no login form \(page saved to debug/login-page.html\)")):
            self.login()
        dump.assert_called_once_with(self.page, "login-page")


class ShortNameTest(unittest.TestCase):
    def test_examples(self):
        for name, short in {
            "Parallel Programming on GPUs": "PPG",
            "Natural Language Processing Laboratory": "NLP Lab",
            "NoSQL Databases": "NoSQL Databases",  # already fits
            "Operating Systems": "OS",
            "Biology for Engineers": "BE",
            "Python Lab": "Python Lab",
            "Electromagnetics Lab": "Electromag… Lab",
        }.items():
            with self.subTest(name=name):
                self.assertEqual(bot.short_name(name), short)
                self.assertLessEqual(len(short), 15)


class ChangesTest(unittest.TestCase):
    def test_first_summary_has_nothing_to_compare(self):
        self.assertEqual(bot.changes(None, snapshot(course("CS501", "Operating Systems", 9, 3, 30))), [])

    def test_new_presents_and_absences(self):
        old = snapshot(course("CS501", "Operating Systems", 9, 1, 30, ["02-09-2026"]),
                       course("CS502", "R&D Methods", 5, 0, 30),
                       course("CS503", "Compiler Design", 4, 0, 30))
        new = snapshot(course("CS501", "Operating Systems", 11, 2, 27, ["02-09-2026", "29-10-2026"]),
                       course("CS502", "R&D Methods", 6, 0, 29),
                       course("CS503", "Compiler Design", 4, 0, 30),  # unchanged
                       course("CS504", "Cloud Computing", 2, 0, 30))  # not in the old snapshot
        self.assertEqual(bot.changes(old, new), [
            "• Operating Systems: +2 present, absent 29-10-2026",
            "• R&amp;D Methods: +1 present",
        ])


class SummaryTest(unittest.TestCase):
    def state(self, *courses):
        return {"fetched_at": "2026-10-28T19:00:00", "data": snapshot(*courses)}

    def test_table_fits_a_phone(self):
        text = bot.summary(self.state(course("CS501", "Parallel Programming on GPUs", 9, 3, 30),
                                      course("CS502", "Natural Language Processing Laboratory", 0, 0, 12),
                                      course("CS503", "NoSQL Databases", 17, 1, 20)))
        table = html.unescape(text.split("<pre>")[1].split("</pre>")[0])
        self.assertLessEqual(max(map(len, table.splitlines())), 31)

    def test_first_line_carries_the_verdict(self):
        behind = course("CS501", "Parallel Programming on GPUs", 9, 3, 30)
        fine = course("CS502", "Operating Systems", 12, 1, 30)
        self.assertEqual(bot.summary(self.state(behind, fine)).splitlines()[0],
                         f"📊 <b>Attendance</b> — 1 course below {REQUIRED}%")
        self.assertEqual(bot.summary(self.state(fine)).splitlines()[0], "📊 <b>Attendance</b> — all on track")

    def test_course_names_are_escaped(self):
        text = bot.summary(self.state(course("CS501", "R&D <Methods>", 9, 3, 30)))
        self.assertIn("⚠️ <b>R&amp;D &lt;Methods&gt;</b>", text)


class StateTest(unittest.TestCase):
    def setUp(self):
        bot.STATE_FILE.unlink(missing_ok=True)

    def test_round_trip(self):
        bot.save_state({"offset": 42})
        self.assertEqual(bot.load_state(), {"offset": 42})
        self.assertEqual(bot.STATE_FILE.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in bot.STATE_FILE.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_missing_file_starts_fresh(self):
        self.assertEqual(bot.load_state(), {"offset": 0})

    def test_corrupt_file_starts_fresh(self):
        bot.STATE_FILE.write_text('{"offset": 4')  # cut off mid-write
        with mock.patch.object(bot, "log") as log:
            self.assertEqual(bot.load_state(), {"offset": 0})
        log.assert_called_once()


def iso(when):
    return when.isoformat(timespec="seconds")


class BotTestCase(unittest.TestCase):
    """Fixed settings, so a local .env (DAILY_AT, the chat ID) can't change the results."""

    day = datetime(2026, 10, 29)

    def setUp(self):
        for name, value in (("DAILY_AT", "19:00"), ("CHAT_ID", "1234"), ("PUSH_TOKEN", "t" * 32)):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bot.STATE_FILE.unlink(missing_ok=True)

    def at(self, hhmm, days=0):
        return bot.at_time(hhmm, self.day) + timedelta(days=days)


class FetchDueTest(BotTestCase):
    def due(self, now, **state):
        return bot.fetch_due(state, now)

    def test_once_a_day_from_half_an_hour_before_the_summary(self):
        yesterday = iso(self.at("18:40", -1))
        self.assertFalse(self.due(self.at("18:29"), fetched_at=yesterday))
        self.assertTrue(self.due(self.at("18:30"), fetched_at=yesterday))
        self.assertFalse(self.due(self.at("21:00"), fetched_at=iso(self.at("18:31"))))
        self.assertFalse(self.due(self.at("09:00", 1), fetched_at=iso(self.at("18:31"))))

    def test_first_check_and_missed_evenings_are_due_straight_away(self):
        self.assertTrue(self.due(self.at("09:00")))
        self.assertTrue(self.due(self.at("09:00"), fetched_at=iso(self.at("18:40", -2))))

    def test_refresh_is_due_even_after_todays_check(self):
        self.assertTrue(self.due(self.at("21:00"), fetched_at=iso(self.at("18:31")),
                                 refresh_wanted=iso(self.at("20:59"))))

    def test_after_a_failure_waits_an_hour_then_tries_once_more(self):
        # Every try is a login from your own connection, so a broken login mustn't retry all evening
        stale = iso(self.at("18:40", -1))
        once = [iso(self.at("18:30"))]
        self.assertFalse(self.due(self.at("19:00"), fetched_at=stale, fetch_failed=once))
        self.assertTrue(self.due(self.at("19:30"), fetched_at=stale, fetch_failed=once))
        twice = once + [iso(self.at("19:30"))]
        self.assertFalse(self.due(self.at("23:00"), fetched_at=stale, fetch_failed=twice))
        self.assertTrue(self.due(self.at("18:30", 1), fetched_at=stale, fetch_failed=twice))  # a new day


class DailySummaryTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.old = snapshot(course("CS501", "Operating Systems", 9, 3, 30))
        self.new = snapshot(course("CS501", "Operating Systems", 10, 3, 29))

    def test_sent_once_todays_numbers_are_in(self):
        state = {"offset": 0, "daily_snapshot": self.old, "data": self.new, "fetched_at": iso(self.at("18:35"))}
        self.assertTrue(bot.daily_due(state, self.at("19:00")))
        with mock.patch.object(bot, "send") as send:
            bot.send_daily(state, self.at("19:00"))
        self.assertIn("• Operating Systems: +1 present", send.call_args.args[0])
        saved = bot.load_state()
        self.assertEqual(saved["daily_snapshot"], self.new)
        self.assertFalse(bot.daily_due(saved, self.at("20:00")))

    def test_waits_for_todays_numbers_and_says_so_once_if_they_dont_come(self):
        state = {"offset": 0, "daily_snapshot": self.old, "data": self.old, "fetched_at": iso(self.at("18:35", -1))}
        with mock.patch.object(bot, "send") as send:
            bot.send_daily(state, self.at("19:00"))
            send.assert_not_called()  # the laptop may still check this evening
            bot.send_daily(state, self.at("22:00"))
            bot.send_daily(state, self.at("22:05"))
        send.assert_called_once()
        self.assertIn("hasn't checked the portal since 18:30", send.call_args.args[0])
        self.assertTrue(bot.daily_due(state, self.at("22:10")))  # the summary still comes with the numbers

    def test_telegram_down_retries_with_the_same_changes(self):
        state = {"offset": 0, "daily_snapshot": self.old, "data": self.new, "fetched_at": iso(self.at("18:35"))}
        with mock.patch.object(bot, "send", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.URLError):
                bot.send_daily(state, self.at("19:00"))
        self.assertNotIn("last_daily", state)
        self.assertEqual(state["daily_snapshot"], self.old)


COURSE_PAGE = attendance.BASE + "index.php?option=com_studentdashboard&task=attendencelist&courseId=CS501"


class PushTest(BotTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("send", mock.Mock()), ("log", mock.Mock()), ("daily_due", lambda *a: False)):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_asks_for_the_course_pages_then_takes_them(self):
        state = {"offset": 0, "fetch_failed": [iso(self.at("18:30"))]}
        self.assertEqual(bot.on_push(state, {"dashboard": DASHBOARD}), {"done": False, "need": [COURSE_PAGE]})
        self.assertNotIn("data", state)
        self.assertEqual(bot.on_push(state, {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: DETAIL}}),
                         {"done": True, "courses": 2})
        first, second = state["data"]["courses"]
        self.assertEqual((first["present"], first["absent"], first["remaining"], first["can_skip"]), (9, 3, 30, 3))
        self.assertNotIn("present", second)  # no link to a detail page
        self.assertNotIn("fetch_failed", state)
        self.assertEqual(bot.load_state()["data"], state["data"])
        bot.send.assert_not_called()  # nobody asked, and the daily summary isn't due

    def test_answers_a_refresh(self):
        state = {"offset": 0, "refresh_wanted": iso(self.at("10:00"))}
        bot.on_push(state, {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: DETAIL}})
        self.assertNotIn("refresh_wanted", state)
        self.assertIn("<pre>", bot.send.call_args.args[0])

    def test_sends_the_daily_summary_when_its_waiting_for_these_numbers(self):
        with (mock.patch.object(bot, "daily_due", return_value=True),
              mock.patch.object(bot, "send_daily") as send_daily):
            bot.on_push({"offset": 0}, {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: DETAIL}})
        send_daily.assert_called_once()

    def test_wrong_page_is_saved_and_reported(self):
        state = {"offset": 0, "refresh_wanted": iso(self.at("10:00"))}
        reply = bot.on_push(state, {"dashboard": "<html><form id='login'></form></html>"})
        self.assertFalse(reply["done"])
        self.assertIn("isn't the dashboard", reply["error"])
        self.assertTrue((attendance.DEBUG_DIR / "pushed-dashboard.html").exists())
        self.assertEqual(len(state["fetch_failed"]), 1)
        self.assertNotIn("refresh_wanted", state)  # one try per /refresh
        self.assertIn("Send /refresh to try again", bot.send.call_args.args[0])

    def test_unreadable_course_page_is_a_failure(self):
        state = {"offset": 0}
        reply = bot.on_push(state, {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: "<html>Session expired</html>"}})
        self.assertIn("Couldn't read the attendance page for CS501", reply["error"])
        self.assertNotIn("data", state)

    def test_failure_report_from_the_extension(self):
        state = {"offset": 0}
        bot.on_failed(state, {"error": "reCAPTCHA didn't pass", "url": "x", "html": "<html>captcha</html>"})
        self.assertTrue((attendance.DEBUG_DIR / "extension-failed.html").exists())
        message = bot.send.call_args.args[0]
        self.assertIn("reCAPTCHA didn&#x27;t pass", message)
        self.assertIn("try again in an hour", message)
        self.assertEqual(len(bot.load_state()["fetch_failed"]), 1)

    def test_bot_never_contacts_the_portal(self):
        # The portal's firewall banned the bot's server: only the extension, in your browser, logs in
        source = (HERE / "bot.py").read_text()
        for name in ("fetch_attendance", "session_alive", "browser_login", "http_session"):
            self.assertNotIn(name, source)


class ServerTest(BotTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("CHAT_ID", ""), ("log", mock.Mock())):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = {"offset": 0}
        server = bot.serve(self.state, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, path, body=None, token="t" * 32, raw=None):
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + path, data, headers), timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_due(self):
        status, body = self.call("/due")
        self.assertEqual(status, 200)
        self.assertIs(body["fetch"], True)
        self.assertEqual((body["login_url"], body["dashboard_url"]), (attendance.LOGIN_URL, attendance.DASHBOARD_URL))

    def test_push_round_trip(self):
        status, body = self.call("/push", {"dashboard": DASHBOARD})
        self.assertEqual((status, body), (200, {"done": False, "need": [COURSE_PAGE]}))
        status, body = self.call("/push", {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: DETAIL}})
        self.assertEqual((status, body), (200, {"done": True, "courses": 2}))
        self.assertEqual(self.state["data"]["courses"][0]["present"], 9)

    def test_needs_the_token(self):
        self.assertEqual(self.call("/due", token="wrong")[0], 401)
        self.assertEqual(self.call("/due", token=None)[0], 401)
        self.assertEqual(self.call("/push", {"dashboard": DASHBOARD}, token="wrong")[0], 401)
        with mock.patch.object(bot, "PUSH_TOKEN", ""):
            self.assertEqual(self.call("/due", token="")[0], 401)  # no token set: nothing gets in
        self.assertNotIn("data", self.state)

    def test_bad_requests(self):
        self.assertEqual(self.call("/nope")[0], 404)
        self.assertEqual(self.call("/push", raw=b"not json")[0], 400)
        self.assertEqual(self.call("/push", raw=b"[1, 2]")[0], 400)


class HandleTest(BotTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(bot, "send")
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def handle(self, text, state, chat=1234):
        bot.handle({"chat": {"id": chat}, "text": text}, state)
        return self.send.call_args.args[0] if self.send.called else None

    def numbers(self, age):
        return {"offset": 0, "fetched_at": iso(datetime.now() - age),
                "data": snapshot(course("CS501", "Operating Systems", 9, 3, 30))}

    def test_refresh_asks_the_laptop(self):
        self.assertIn("Asked your laptop", self.handle("/refresh", {"offset": 0}))
        self.assertIn("refresh_wanted", bot.load_state())

    def test_shows_the_latest_numbers(self):
        self.assertTrue(self.handle("/attendance", self.numbers(timedelta(hours=2))).startswith("📊"))

    def test_old_numbers_say_how_old(self):
        self.assertTrue(self.handle("hi", self.numbers(timedelta(days=2))).startswith("⚠️ These numbers are from"))

    def test_no_numbers_yet(self):
        self.assertIn("No numbers yet", self.handle("/attendance", {"offset": 0}))

    def test_ignores_other_chats(self):
        with mock.patch.object(bot, "log"):
            self.assertIsNone(self.handle("/refresh", {"offset": 0}, chat=999))


class ConfigTest(unittest.TestCase):
    def test_settings_are_checked_at_startup(self):
        class PassedValidation(Exception):
            pass

        cases = [("19:00", "t" * 32, True), ("7:30", "t" * 32, True), ("25:00", "t" * 32, False),
                 ("19:60", "t" * 32, False), ("7pm", "t" * 32, False), ("19:00", "", False), ("19:00", "short", False)]
        for daily_at, push_token, valid in cases:
            with (self.subTest(DAILY_AT=daily_at, PUSH_TOKEN=push_token),
                  mock.patch.object(bot, "TOKEN", "test"),
                  mock.patch.object(bot, "DAILY_AT", daily_at),
                  mock.patch.object(bot, "PUSH_TOKEN", push_token),
                  mock.patch.object(bot, "tg", side_effect=PassedValidation)):
                with self.assertRaises(PassedValidation if valid else SystemExit):
                    bot.main()

    def test_dependencies_agree(self):
        # bot.py imports attendance.py, and the Docker image installs packages itself,
        # so both need everything attendance.py declares
        def declared(script):
            line = re.search(r"^# dependencies = (.*)$", (HERE / script).read_text(), re.M).group(1)
            return {re.split(r"[<>=~!]", d)[0] for d in ast.literal_eval(line)}

        needed = declared("attendance.py")
        self.assertLessEqual(needed, declared("bot.py"))
        dockerfile = (HERE / "Dockerfile").read_text()
        for package in needed:
            self.assertRegex(dockerfile, rf"pip install .*\b{package}\b")


if __name__ == "__main__":
    unittest.main()
