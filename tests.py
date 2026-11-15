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
from datetime import datetime, timedelta
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

    def test_security_code_stops_a_headless_login_before_submitting(self):
        # The code is for a person to read; a script that guessed at it would look like a bot
        self.page.locator.return_value.count.return_value = 1
        self.page.locator.return_value.is_visible.return_value = True
        with self.assertRaisesRegex(attendance.PortalError, "security code. Run attendance.py --show"):
            self.login()
        self.page.click.assert_not_called()


class ShortNameTest(unittest.TestCase):
    def test_examples(self):
        for name, short in {
            "Parallel Programming on GPUs": "PPG",
            "Natural Language Processing Laboratory": "NLP Lab",
            "NoSQL Databases": "NoSQL",  # a nickname
            "Data Mining": "Data Mining",  # already fits
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
            "• Cloud Computing: new on the portal",
        ])

    def test_identical_reads_have_no_changes(self):
        read = snapshot(course("CS501", "Operating Systems", 9, 1, 30, ["02-09-2026"]))
        self.assertEqual(bot.changes(read, read), [])

    def test_every_difference_gets_a_line(self):
        # "No changes" is only said for an empty list, so nothing that moves Skip may go unmentioned
        old = snapshot(course("CS501", "Operating Systems", 9, 1, 30),
                       course("CS502", "R&D Methods", 5, 2, 30, ["01-09-2026", "02-09-2026"]),
                       course("CS503", "Compiler Design", 4, 0, 30),
                       {"code": "CS504", "name": "Cloud Computing", "attendance": 0.0, "detail_url": None})
        new = snapshot(course("CS501", "Operating Systems", 9, 1, 28),       # the plan changed: Skip moves
                       course("CS502", "R&D Methods", 4, 1, 30, ["01-09-2026"]),  # corrected
                       course("CS504", "Cloud Computing", 1, 0, 30))           # details now shown
        self.assertEqual(bot.changes(old, new), [
            "• Operating Systems: classes left 30 → 28",
            "• R&amp;D Methods: present 5 → 4, absent 2 → 1",
            "• Cloud Computing: attendance details now shown",
            "• Compiler Design: no longer on the portal",
        ])

    def test_courses_without_details_and_anything_else_still_count(self):
        def dashboard_only(attendance):
            return {"code": "CS505", "name": "Ethics", "attendance": attendance, "detail_url": None}
        renamed = dict(course("CS501", "Operating Systems", 9, 1, 30), name="Operating Systems I")
        old = snapshot(dashboard_only(80.0), course("CS501", "Operating Systems", 9, 1, 30))
        new = snapshot(dashboard_only(85.0), renamed)
        self.assertEqual(bot.changes(old, new), ["• Ethics: 80% → 85%", "• Operating Systems I: changed on the portal"])
        moved = snapshot(dict(dashboard_only(80.0), detail_url="index.php?x"), course("CS501", "Operating Systems", 9, 1, 30))
        self.assertEqual(bot.changes(old, moved), [])  # only the link moved: no change worth a line


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

    def test_says_what_changed_since_the_last_read_or_that_nothing_did(self):
        state = self.state(course("CS501", "Operating Systems", 12, 1, 30))
        last = datetime(2026, 10, 27, 18, 31)
        changed = bot.summary(state, ["• Operating Systems: +1 present"], since=last)
        self.assertIn("🆕 <b>Since your last read</b> (27 Oct, 18:31)\n• Operating Systems: +1 present", changed)
        same = bot.summary(state, [], since=last)
        self.assertIn("No changes since your last read (27 Oct, 18:31).", same)
        self.assertIn("<pre>", same)  # the full table either way
        first = bot.summary(state)
        self.assertNotIn("since your last read", first.lower())


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
    """Fixed settings, so a local .env (the chat ID, the push token) can't change the results."""

    def setUp(self):
        for name, value in (("CHAT_ID", "1234"), ("PUSH_TOKEN", "t" * 32)):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bot.STATE_FILE.unlink(missing_ok=True)


class ReadSummaryTest(BotTestCase):
    """Every read sends the summary, compared with the last read whose summary went out."""

    def setUp(self):
        super().setUp()
        for name, value in (("send", mock.Mock()), ("log", mock.Mock())):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.old = snapshot(course("CS501", "Operating Systems", 9, 3, 30))
        self.new = snapshot(course("CS501", "Operating Systems", 10, 3, 29))

    def test_first_read_has_nothing_to_compare(self):
        state = {"offset": 0}
        self.assertTrue(bot.accept(state, self.old))
        self.assertNotIn("since your last read", bot.send.call_args.args[0].lower())
        self.assertEqual(state["baseline"]["data"], self.old)

    def test_next_reads_say_what_changed_or_that_nothing_did(self):
        state = {"offset": 0}
        bot.accept(state, self.old)
        bot.accept(state, self.old)
        self.assertIn("No changes since your last read", bot.send.call_args.args[0])
        bot.accept(state, self.new)
        self.assertIn("• Operating Systems: +1 present", bot.send.call_args.args[0])
        self.assertEqual(bot.load_state()["baseline"]["data"], self.new)

    def test_telegram_down_keeps_the_baseline(self):
        # So the changes come with the next read instead of going unmentioned
        state = {"offset": 0}
        bot.accept(state, self.old)
        bot.send.side_effect = urllib.error.URLError("offline")
        self.assertFalse(bot.accept(state, self.new))
        self.assertEqual(state["data"], self.new)  # /attendance has the new numbers meanwhile
        self.assertEqual(state["baseline"]["data"], self.old)
        bot.send.side_effect = None
        self.assertTrue(bot.accept(state, self.new))
        self.assertIn("+1 present", bot.send.call_args.args[0])

    def test_old_state_is_migrated(self):
        state = {"offset": 7, "data": self.old, "fetched_at": "2026-11-15T14:36:00", "daily_snapshot": self.old,
                 "last_daily": "2026-11-14", "daily_notice": "2026-11-13", "reminded": "2026-11-15",
                 "fetch_failed": [], "refresh_wanted": "2026-11-15T14:00:00"}
        self.assertEqual(bot.migrate(state), {"offset": 7, "data": self.old, "fetched_at": "2026-11-15T14:36:00",
                                              "baseline": {"data": self.old, "fetched_at": "2026-11-15T14:36:00"}})


COURSE_PAGE = attendance.BASE + "index.php?option=com_studentdashboard&task=attendencelist&courseId=CS501"


class PushTest(BotTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("send", mock.Mock()), ("log", mock.Mock())):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_asks_for_the_course_pages_then_takes_them(self):
        state = {"offset": 0}
        self.assertEqual(bot.on_push(state, {"dashboard": DASHBOARD}), {"done": False, "need": [COURSE_PAGE]})
        self.assertNotIn("data", state)
        self.assertEqual(bot.on_push(state, {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: DETAIL}}),
                         {"done": True, "courses": 2, "sent": True})
        first, second = state["data"]["courses"]
        self.assertEqual((first["present"], first["absent"], first["remaining"], first["can_skip"]), (9, 3, 30, 3))
        self.assertNotIn("present", second)  # no link to a detail page
        self.assertEqual(bot.load_state()["data"], state["data"])
        self.assertIn("<pre>", bot.send.call_args.args[0])  # every read gets the summary

    def test_wrong_page_is_saved_and_reported(self):
        state = {"offset": 0}
        reply = bot.on_push(state, {"dashboard": "<html><form id='login'></form></html>"})
        self.assertFalse(reply["done"])
        self.assertIn("isn't the dashboard", reply["error"])
        self.assertTrue((attendance.DEBUG_DIR / "pushed-dashboard.html").exists())
        self.assertNotIn("data", state)
        self.assertIn("Click the extension's icon on a portal page", bot.send.call_args.args[0])

    def test_unreadable_course_page_is_a_failure(self):
        state = {"offset": 0}
        reply = bot.on_push(state, {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: "<html>Session expired</html>"}})
        self.assertIn("Couldn't read the attendance page for CS501", reply["error"])
        self.assertNotIn("data", state)

    def test_failure_report_from_the_extension(self):
        state = {"offset": 0}
        bot.on_failed(state, {"error": "Couldn't load a portal page: <timeout>"})
        message = bot.send.call_args.args[0]
        self.assertIn("Couldn&#x27;t load a portal page: &lt;timeout&gt;", message)
        self.assertIn("Click the extension's icon", message)

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

    def test_portal_addresses(self):
        status, body = self.call("/portal")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"dashboard_url": attendance.DASHBOARD_URL, "referrer": attendance.LOGIN_URL})

    def test_extensions_before_3_get_nothing(self):
        # 1.x logged in by itself (which the portal refuses) and 2.0 read on every page load; both
        # start by asking /due, so without it they stay idle
        self.assertEqual(self.call("/due")[0], 404)

    def test_push_round_trip(self):
        status, body = self.call("/push", {"dashboard": DASHBOARD})
        self.assertEqual((status, body), (200, {"done": False, "need": [COURSE_PAGE]}))
        status, body = self.call("/push", {"dashboard": DASHBOARD, "pages": {COURSE_PAGE: DETAIL}})
        self.assertEqual((status, body), (200, {"done": True, "courses": 2, "sent": False}))  # no chat set
        self.assertEqual(self.state["data"]["courses"][0]["present"], 9)

    def test_needs_the_token(self):
        self.assertEqual(self.call("/portal", token="wrong")[0], 401)
        self.assertEqual(self.call("/portal", token=None)[0], 401)
        self.assertEqual(self.call("/push", {"dashboard": DASHBOARD}, token="wrong")[0], 401)
        with mock.patch.object(bot, "PUSH_TOKEN", ""):
            self.assertEqual(self.call("/portal", token="")[0], 401)  # no token set: nothing gets in
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

    def test_refresh_says_its_gone(self):
        # The bot can't start a read any more; say so instead of quietly showing old numbers
        self.assertEqual(self.handle("/refresh", {"offset": 0}), bot.GONE)

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

        for push_token, valid in (("t" * 32, True), ("", False), ("short", False)):
            with (self.subTest(PUSH_TOKEN=push_token),
                  mock.patch.object(bot, "TOKEN", "test"),
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
