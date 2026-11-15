#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""End-to-end test of the Chrome extension: the real extension in Chromium, the bot's real web
server, and a fake portal that behaves like the real one (invisible captcha, verification step,
Referer rule). The test logs in by hand, the way you do, and checks that the extension only reads.
Nothing here reaches the real portal or Telegram.

    uv run test_extension.py

Needs Chromium rather than Google Chrome, which stopped loading extensions from the command line in
version 137. It uses /usr/bin/chromium-browser, or CHROMIUM=/path/to/chromium, or else Playwright's
own Chromium (install it with: uv run --with playwright python -m playwright install chromium)."""

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
EXTENSION = HERE / "extension"

import attendance  # noqa: E402
import bot  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from tests import DASHBOARD, DETAIL  # noqa: E402  (hand-made pages, no personal data)

USN, DOB, LAST4 = "1MS00XX000", "2004-05-16", "4321"
TOKEN = "e2e-" + "t" * 32
CHROMIUM = os.environ.get("CHROMIUM") or ("/usr/bin/chromium-browser" if Path("/usr/bin/chromium-browser").exists() else None)

LOGIN_PAGE = """<html><body>
<form method="post" action="index.php">
  <input id="username" name="username">
  <select id="dd">{days}</select>
  <select id="mm">{months}</select>
  <select id="yyyy">{years}</select>
  <input type="hidden" id="passwd" name="passwd">
  <input type="hidden" id="captcha-response" name="captcha">
  <!--CODE-->
  <input type="submit" value="Login">
</form>
<script>
  function putdate() {{
    var v = function (id) {{ return document.getElementById(id).value.trim(); }};
    document.getElementById('passwd').value = v('yyyy') + '-' + v('mm') + '-' + v('dd');
  }}
  // Stands in for the invisible reCAPTCHA, which fills this field a moment after the page loads
  setTimeout(function () {{ document.getElementById('captcha-response').value = 'ok'; }}, CAPTCHA_MS);
</script>
</body></html>""".format(
    # Like the real page: day values carry a trailing space, labels don't
    days="".join(f"<option value='{d:02d} '>{d:02d}</option>" for d in range(1, 32)),
    months="".join(f"<option value='{m:02d}'>{m:02d}</option>" for m in range(1, 13)),
    years="".join(f"<option value='{y}'>{y}</option>" for y in range(1998, 2010)),
)

VERIFY_PAGE = """<html><body>
<form method="post" action="index.php?task=verify">
  <select name="type"><option value="">Select</option><option value="M">Mother Mobile</option>
    <option value="F">Father Mobile</option></select>
  <input name="d1" maxlength="1"><input name="d2" maxlength="1">
  <input name="d3" maxlength="1"><input name="d4" maxlength="1">
  <button type="submit">Submit</button>
</form>
</body></html>"""


class FakePortal(BaseHTTPRequestHandler):
    """Just enough of the portal: login, verification, then pages that need the session cookie and
    a Referer from the portal (without one, the real portal logs the session out)."""

    requests = []  # (method, path, referer)
    captcha_ms = 800
    code = "K7Q2P"  # the security code to ask for, like the real login page since November 2026
    broken_dashboard = False  # serve something else at the dashboard's address
    course_page_delay = 0  # seconds, to leave time to walk away mid-read

    def login_page(self, headers=()):
        box = ('<img alt="Security code" src="data:,">'
               '<input id="cs_cap" name="cs_cap" placeholder="Type the 5 characters above">') if self.code else ""
        self.page(LOGIN_PAGE.replace("CAPTCHA_MS", str(self.captcha_ms)).replace("<!--CODE-->", box),
                  headers=headers)

    def log_message(self, *args):
        pass

    def page(self, body, status=200, headers=()):
        data = body.encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser stopped waiting, e.g. you moved on to another page mid-read

    def cookies(self):
        return dict(c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c)

    def do_GET(self):
        if self.path == "/favicon.ico":  # the browser asks for it by itself
            return self.page("", 404)
        self.requests.append(("GET", self.path, self.headers.get("Referer")))
        query = parse_qs(urlsplit(self.path).query)
        if "task" not in query and "ksign" not in query:
            return self.login_page()
        if self.cookies().get("session") != "good":
            return self.login_page()
        if query.get("ksign") == ["hop"]:  # a landing page that moves on by script, as some do
            return self.page("<html><body><script>setTimeout(function () {"
                             " location.replace('index.php?ksign=landed'); }, 300);</script></body></html>")
        if "ksign" in query:
            return self.page("<html><body>Welcome</body></html>")
        origin = f"http://{self.headers['Host']}/"
        if not (self.headers.get("Referer") or "").startswith(origin):
            return self.login_page(headers=[("Set-Cookie", "session=; Max-Age=0")])
        if query["task"] == ["dashboard"]:
            return self.page("<html><body>Something else</body></html>" if self.broken_dashboard else DASHBOARD)
        time.sleep(self.course_page_delay)
        self.page(DETAIL)

    def do_POST(self):
        self.requests.append(("POST", self.path, self.headers.get("Referer")))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        if "task=verify" in self.path:
            digits = "".join(form.get(f"d{i}", "") for i in range(1, 5))
            if self.cookies().get("stage") == "verify" and form.get("type") == "F" and digits == LAST4:
                return self.page("", 302, [("Location", "index.php?ksign=abc"), ("Set-Cookie", "session=good")])
            return self.page(VERIFY_PAGE)
        right_code = not self.code or form.get("cs_cap") == self.code
        if form.get("username") == USN and form.get("passwd") == DOB and form.get("captcha") and right_code:
            return self.page(VERIFY_PAGE, headers=[("Set-Cookie", "stage=verify")])
        self.login_page()


def start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ExtensionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.portal = start(FakePortal)
        base = f"http://127.0.0.1:{cls.portal.server_address[1]}/newparents/"
        cls.sent = []
        cls.patches = [mock.patch.object(*p) for p in [
            (attendance, "BASE", base),
            (attendance, "LOGIN_URL", base + "index.php"),
            (attendance, "DASHBOARD_URL",
             base + "index.php?option=com_studentdashboard&controller=studentdashboard&task=dashboard"),
            (attendance, "DEBUG_DIR", tmp / "debug"),
            (bot, "STATE_FILE", tmp / ".bot-state.json"),
            (bot, "PUSH_TOKEN", TOKEN),
            (bot, "CHAT_ID", "1234"),
            (bot, "send", cls.sent.append),
            (bot, "tg", mock.Mock(side_effect=AssertionError("must not reach Telegram"))),
            (bot, "log", lambda *args: None),
        ]]
        for p in cls.patches:
            p.start()
        cls.state = {"offset": 0}
        cls.bot = bot.serve(cls.state, "127.0.0.1", 0)

        # The extension as shipped, pointed at the local bot and portal
        ext = tmp / "extension"
        shutil.copytree(EXTENSION, ext)
        script = (ext / "background.js").read_text()
        local = f'const BOT = "http://127.0.0.1:{cls.bot.server_address[1]}";'
        script = script.replace('const BOT = "https://attendance-bot-6767.fly.dev";', local)
        script = script.replace('const PORTAL = "https://parents.msrit.edu/newparents/";', f'const PORTAL = "{base}";')
        (ext / "background.js").write_text(script)
        manifest = (ext / "manifest.json").read_text()
        manifest = manifest.replace('"https://parents.msrit.edu/*", "https://attendance-bot-6767.fly.dev/*"',
                                    '"http://127.0.0.1/*"')
        (ext / "manifest.json").write_text(manifest)
        assert local in script and f'"{base}"' in script and "127.0.0.1" in manifest

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch_persistent_context(
            tmp / "profile", executable_path=CHROMIUM, channel=None if CHROMIUM else "chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        cls.worker = (cls.browser.service_workers or [cls.browser.wait_for_event("serviceworker")])[0]
        cls.js("chrome.storage.local.set(%s)" % {"token": TOKEN})  # all it needs: you do the logging in
        cls.tab = None  # your logged-in portal tab, shared by the tests in order

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.bot.shutdown()
        cls.portal.shutdown()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    @classmethod
    def js(cls, expression):
        return cls.worker.evaluate(f"(async () => {expression})()")

    def setUp(self):
        FakePortal.requests.clear()
        self.sent.clear()

    def log_in_by_hand(self):
        """What you do in a normal tab. The extension has no part in it."""
        page = self.browser.new_page()
        page.goto(attendance.LOGIN_URL)
        page.fill("#username", USN)
        yyyy, mm, dd = DOB.split("-")
        page.select_option("#dd", label=dd)
        page.select_option("#mm", value=mm)
        page.select_option("#yyyy", value=yyyy)
        page.evaluate("putdate()")  # the real page runs it when the year changes
        page.fill("#cs_cap", FakePortal.code)
        page.wait_for_function("document.getElementById('captcha-response').value !== ''")
        with page.expect_navigation():
            page.click("input[type=submit]")
        page.select_option("select[name=type]", value="F")
        for i, digit in enumerate(LAST4, 1):
            page.fill(f"input[name=d{i}]", digit)
        with page.expect_navigation():
            page.click("button[type=submit]")
        type(self).tab = page
        return page

    def history(self):
        return self.js("(await chrome.storage.local.get()).history || []")

    def wait_for_read(self, before):
        """The newest history entry once there are more than `before`."""
        for _ in range(150):
            history = self.history()
            if len(history) > before:
                return history[0]
            time.sleep(0.2)
        self.fail("the extension didn't finish a read")

    def tab_id(self):
        return self.js("(await chrome.tabs.query({})).find((t) => (t.url || '').includes('ksign=')).id")

    def portal_gets(self):
        return [path for method, path, _ in FakePortal.requests if method == "GET" and "task=" in path]

    def test_1_reads_after_you_log_in_by_hand(self):
        before, pages = len(self.history()), len(self.browser.pages)
        self.log_in_by_hand()
        entry = self.wait_for_read(before)
        self.assertEqual((entry["mode"], entry["ok"], entry["message"]),
                         ("auto", True, "Sent attendance for 2 courses to the bot."))
        first, second = self.state["data"]["courses"]
        self.assertEqual((first["present"], first["absent"], first["remaining"], first["can_skip"]), (9, 3, 30, 3))
        self.assertNotIn("present", second)
        # The only POSTs are the two you made; the extension opened no tab and only read pages
        self.assertEqual(sum(1 for method, _, _ in FakePortal.requests if method == "POST"), 2)
        self.assertEqual(len(self.browser.pages), pages + 1)
        self.assertEqual(len(self.portal_gets()), 2)  # the dashboard and the one course page
        for method, path, referer in FakePortal.requests:
            if "task=" in path:
                self.assertTrue(referer and referer.startswith(attendance.BASE), (path, referer))
        self.assertEqual(self.js(f"chrome.action.getBadgeText({{tabId: {self.tab_id()}}})"), "✓")
        self.assertIn("Welcome", self.tab.content())  # your session is still alive

    def test_2_nothing_is_read_when_no_read_is_due(self):
        before = len(self.history())
        self.tab.reload()
        time.sleep(3)
        self.assertEqual(self.portal_gets(), [])
        self.assertEqual(len(self.history()), before)

    def test_3_clicking_the_icon_on_a_portal_page_reads_now(self):
        fetched = self.state["fetched_at"]
        time.sleep(1.1)  # fetched_at has whole seconds
        self.js(f"onIconClick(await chrome.tabs.get({self.tab_id()}))")
        self.assertEqual(self.history()[0]["mode"], "manual")
        self.assertTrue(self.history()[0]["ok"])
        self.assertNotEqual(self.state["fetched_at"], fetched)
        self.assertTrue(any("<pre>" in text for text in self.sent), self.sent)  # the summary, right away

    def test_4_refresh_reads_at_your_next_portal_page_and_answers(self):
        self.state["refresh_wanted"] = "2026-11-15T14:00:00"
        before = len(self.history())
        self.tab.reload()
        self.assertTrue(self.wait_for_read(before)["ok"])
        self.assertNotIn("refresh_wanted", self.state)
        self.assertTrue(any("<pre>" in text for text in self.sent), self.sent)

    def test_5_leaving_the_page_mid_read_isnt_a_failure(self):
        self.state["refresh_wanted"] = "2026-11-15T14:00:00"
        failures = len(self.state.get("fetch_failed", []))
        before = len(self.history())
        FakePortal.course_page_delay = 3
        try:
            self.tab.reload()
            for _ in range(50):  # until the read has got as far as the course page
                if any("courseId" in path for path in self.portal_gets()):
                    break
                time.sleep(0.1)
            self.tab.goto(attendance.BASE + "index.php?ksign=elsewhere")  # you click on to another page
            entry = self.wait_for_read(before)
        finally:
            FakePortal.course_page_delay = 0
        self.assertFalse(entry["ok"])
        self.assertIn("left the page", entry["message"])
        self.assertEqual(len(self.state.get("fetch_failed", [])), failures)
        self.assertEqual(self.sent, [])
        self.assertIn("refresh_wanted", self.state)  # still wanted: the next page tries again
        self.state.pop("refresh_wanted")

    def test_6_a_failed_read_pauses_automatic_reads_until_one_works(self):
        self.state["refresh_wanted"] = "2026-11-15T14:00:00"
        before = len(self.history())
        FakePortal.broken_dashboard = True
        try:
            self.tab.reload()
            entry = self.wait_for_read(before)
        finally:
            FakePortal.broken_dashboard = False
        self.assertFalse(entry["ok"])
        self.assertIn("Automatic reads are paused", entry["message"])
        self.assertTrue(self.js("(await chrome.storage.local.get()).paused"))
        self.assertTrue(any("Couldn't read your attendance" in text for text in self.sent), self.sent)
        # Paused means paused: the next page, with a read wanted, doesn't try again
        self.state["refresh_wanted"] = "2026-11-15T14:05:00"
        FakePortal.requests.clear()
        self.tab.reload()
        time.sleep(3)
        self.assertEqual(self.portal_gets(), [])
        # Clicking the icon works, and that ends the pause: the next wanted read happens by itself
        self.js(f"onIconClick(await chrome.tabs.get({self.tab_id()}))")
        self.assertTrue(self.history()[0]["ok"])
        self.assertNotIn("paused", self.js("chrome.storage.local.get()"))
        self.state["refresh_wanted"] = "2026-11-15T14:10:00"
        before = len(self.history())
        self.tab.reload()
        self.assertEqual((self.wait_for_read(before)["mode"]), "auto")

    def test_7_one_read_per_login_even_with_a_redirect_or_two_tabs(self):
        # A landing page that moves on by script: only the page it lands on is read
        self.state["refresh_wanted"] = "2026-11-15T14:00:00"
        before = len(self.history())
        self.tab.goto(attendance.BASE + "index.php?ksign=hop")
        self.assertTrue(self.wait_for_read(before)["ok"])
        time.sleep(1.5)  # in case a second read were coming
        self.assertEqual(sum(1 for path in self.portal_gets() if "task=dashboard" in path), 1)
        self.assertEqual(len(self.history()), before + 1)
        landed = self.js("(await chrome.tabs.query({})).find((t) => (t.url || '').includes('ksign=landed')).id")
        self.assertEqual(self.js(f"chrome.action.getBadgeText({{tabId: {landed}}})"), "✓")

        # Two portal tabs loading at the same moment
        other = self.browser.new_page()
        try:
            other.goto(attendance.BASE + "index.php?ksign=abc")
            time.sleep(1.5)  # not due yet, so that load read nothing
            FakePortal.requests.clear()
            self.state["refresh_wanted"] = "2026-11-15T14:05:00"
            before = len(self.history())
            for page in (self.tab, other):
                page.evaluate("location.reload()")
            self.wait_for_read(before)
            time.sleep(2)
            self.assertEqual(sum(1 for path in self.portal_gets() if "task=dashboard" in path), 1)
            self.assertEqual(len(self.history()), before + 1)
        finally:
            other.close()

    def test_8_no_reads_before_login_or_outside_this_terms_portal(self):
        self.state["refresh_wanted"] = "2026-11-15T14:00:00"
        before = len(self.history())
        origin = attendance.BASE.rsplit("/newparents/", 1)[0]
        page = self.browser.new_page()
        try:
            for url in (attendance.LOGIN_URL, origin + "/webfiles/", origin + "/newparentseven/index.php?ksign=x"):
                page.goto(url)
            # A ksign address that shows the login page, as it does once the session has ended
            self.browser.clear_cookies()
            page.goto(attendance.BASE + "index.php?ksign=abc")
            time.sleep(3)
            self.assertEqual(self.portal_gets(), [])
            self.assertEqual(len(self.history()), before)
            # Clicking the icon there says to log in first, and reads nothing
            tab_id = self.js("(await chrome.tabs.query({})).find((t) => (t.url || '').includes('ksign=abc')).id")
            self.js(f"onIconClick(await chrome.tabs.get({tab_id}))")
            self.assertIn("Log in to the portal first", self.history()[0]["message"])
            self.assertEqual(self.js(f"chrome.action.getBadgeText({{tabId: {tab_id}}})"), "?")
            self.assertEqual(self.portal_gets(), [])
        finally:
            page.close()
            self.state.pop("refresh_wanted", None)

    def test_9_only_reads_and_forgets_what_older_versions_kept(self):
        # The portal refuses logins done by an extension; this one never fills in, clicks or opens anything
        source = (EXTENSION / "background.js").read_text()
        for forbidden in (".click(", ".submit(", "dispatchEvent", "tabs.create", "tabs.remove", "tabs.update",
                          ".value =", ".focus(", "outerHTML"):
            self.assertNotIn(forbidden, source)
        # Older versions kept your portal login, and an "auto" switch that meant logging in by itself
        self.js("chrome.storage.local.set({usn: 'x', dob: 'y', last4: 'z', runs: [1], auto: false})")
        self.js("forgetPortalLogin()")
        stored = self.js("chrome.storage.local.get()")
        for key in ("usn", "dob", "last4", "runs", "auto"):
            self.assertNotIn(key, stored)
        self.assertEqual(stored["token"], TOKEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
