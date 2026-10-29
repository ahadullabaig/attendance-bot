#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""Offline tests for attendance.py and bot.py. Run with: uv run tests.py

These never reach the portal or Telegram: a portal login would end the running bot's session, and
a second Telegram poller would conflict with it. setUpModule turns any such call into a test error
and points every file the code writes at a temporary folder. The pages below are hand-made from the
parsers' selectors, not saved from the portal, so they hold no personal data."""

import ast
import html
import itertools
import re
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

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


class DailySummaryTest(unittest.TestCase):
    def setUp(self):
        self.old = snapshot(course("CS501", "Operating Systems", 9, 3, 30))
        self.new = snapshot(course("CS501", "Operating Systems", 10, 3, 29))
        self.state = {"offset": 0, "daily_snapshot": self.old}

    def fake_refresh(self, state):
        state.update(data=self.new, fetched_at="2026-10-29T19:00:00")

    def test_sent(self):
        with (mock.patch.object(bot, "refresh", self.fake_refresh),
              mock.patch.object(bot, "send") as send):
            bot.send_daily(self.state)
        self.assertIn("• Operating Systems: +1 present", send.call_args.args[0])
        self.assertEqual(bot.load_state()["daily_snapshot"], self.new)

    def test_telegram_down_keeps_changes_for_tomorrow(self):
        with (mock.patch.object(bot, "refresh", self.fake_refresh),
              mock.patch.object(bot, "send", side_effect=urllib.error.URLError("offline"))):
            with self.assertRaises(urllib.error.URLError):
                bot.send_daily(self.state)
        saved = bot.load_state()
        self.assertEqual(saved["last_daily"], date.today().isoformat())  # still not retried all evening
        self.assertEqual(saved["daily_snapshot"], self.old)


class HandleTest(unittest.TestCase):
    def test_portal_error_is_sent_as_html(self):
        # The error text is HTML-escaped, so without HTML parse mode "didn't" shows up as "didn&#x27;t"
        with (mock.patch.object(bot, "CHAT_ID", "1234"),
              mock.patch.object(bot, "send", return_value={"message_id": 7}),
              mock.patch.object(bot, "refresh", side_effect=attendance.PortalError("reCAPTCHA didn't pass")),
              mock.patch.object(bot, "log"),
              mock.patch.object(bot, "tg") as tg):
            bot.handle({"chat": {"id": 1234}, "text": "/refresh"}, {"offset": 0})
        self.assertEqual(tg.call_args.args[0], "editMessageText")
        self.assertEqual(tg.call_args.kwargs.get("parse_mode"), "HTML")

    def test_ignores_other_chats(self):
        with (mock.patch.object(bot, "CHAT_ID", "1234"),
              mock.patch.object(bot, "send") as send,
              mock.patch.object(bot, "log")):
            bot.handle({"chat": {"id": 999}, "text": "/refresh"}, {"offset": 0})
        send.assert_not_called()


class ConfigTest(unittest.TestCase):
    def test_daily_time_is_checked_at_startup(self):
        class PassedValidation(Exception):
            pass

        for value, valid in (("19:00", True), ("7:30", True), ("25:00", False), ("19:60", False), ("7pm", False)):
            with (self.subTest(DAILY_AT=value),
                  mock.patch.object(bot, "TOKEN", "test"),
                  mock.patch.object(bot, "DAILY_AT", value),
                  mock.patch.object(attendance, "load_credentials", side_effect=PassedValidation)):
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
