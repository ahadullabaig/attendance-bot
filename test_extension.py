#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""End-to-end test of the Chrome extension: the real extension in Chromium, the bot's real web
server, and a fake portal that behaves like the real one (invisible captcha, verification step,
Referer rule). The test logs in by hand, the way you do, clicks the icon, and checks that the
extension only reads, and only when clicked. Nothing here reaches the real portal or Telegram.

    uv run test_extension.py

Needs Chromium rather than Google Chrome, which stopped loading extensions from the command line in
version 137. It uses /usr/bin/chromium-browser, or CHROMIUM=/path/to/chromium, or else Playwright's
own Chromium (install it with: uv run --with playwright python -m playwright install chromium)."""

import os
import shutil
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
    detail = DETAIL  # the course page; a test can change it to see a change reported

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
        if "ksign" in query:
            return self.page("<html><body>Welcome</body></html>")
        origin = f"http://{self.headers['Host']}/"
        if not (self.headers.get("Referer") or "").startswith(origin):
            return self.login_page(headers=[("Set-Cookie", "session=; Max-Age=0")])
        if query["task"] == ["dashboard"]:
            return self.page("<html><body>Something else</body></html>" if self.broken_dashboard else DASHBOARD)
        time.sleep(self.course_page_delay)
        self.page(self.detail)

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

    def tab_id(self, part="ksign=", exact=False):
        match = f"t.url === '{part}'" if exact else f"(t.url || '').includes('{part}')"
        return self.js(f"(await chrome.tabs.query({{}})).find((t) => {match}).id")

    def click(self, part="ksign=", exact=False):
        """Click the extension's icon on the tab whose address contains `part`, and wait for it."""
        self.js(f"onIconClick(await chrome.tabs.get({self.tab_id(part, exact)}))")
        return self.history()[0]

    def badge(self, part="ksign="):
        return self.js(f"chrome.action.getBadgeText({{tabId: {self.tab_id(part)}}})")

    def portal_gets(self):
        return [path for method, path, _ in FakePortal.requests if method == "GET" and "task=" in path]

    def test_1_a_click_after_logging_in_by_hand_reads_and_sends_the_summary(self):
        pages = len(self.browser.pages)
        self.log_in_by_hand()
        time.sleep(2)
        self.assertEqual(self.portal_gets(), [])  # logging in alone reads nothing
        entry = self.click()
        self.assertEqual((entry["ok"], entry["message"]),
                         (True, "Sent attendance for 2 courses; the summary is in Telegram."))
        first, second = self.state["data"]["courses"]
        self.assertEqual((first["present"], first["absent"], first["remaining"], first["can_skip"]), (9, 3, 30, 3))
        self.assertNotIn("present", second)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("<pre>", self.sent[0])
        self.assertNotIn("since your last read", self.sent[0].lower())  # the first read
        # The only POSTs are the two you made; the extension opened no tab and only read pages
        self.assertEqual(sum(1 for method, _, _ in FakePortal.requests if method == "POST"), 2)
        self.assertEqual(len(self.browser.pages), pages + 1)
        self.assertEqual(len(self.portal_gets()), 2)  # the dashboard and the one course page
        for method, path, referer in FakePortal.requests:
            if "task=" in path:
                self.assertTrue(referer and referer.startswith(attendance.BASE), (path, referer))
        self.assertEqual(self.badge(), "✓")
        self.assertIn("Welcome", self.tab.content())  # your session is still alive

    def test_2_browsing_the_portal_never_reads_by_itself(self):
        before = len(self.history())
        self.tab.reload()
        self.tab.goto(attendance.BASE + "index.php?ksign=another")
        time.sleep(2)
        self.assertEqual(self.portal_gets(), [])
        self.assertEqual(len(self.history()), before)
        self.assertEqual(self.sent, [])

    def test_3_the_next_click_says_what_changed_since_the_last_read(self):
        self.click()
        self.assertIn("No changes since your last read", self.sent[-1])
        self.assertIn("<pre>", self.sent[-1])  # the full table all the same
        FakePortal.detail = DETAIL.replace("Present[9]", "Present[10]").replace("Still to go[30]", "Still to go[29]")
        try:
            self.click()
        finally:
            FakePortal.detail = DETAIL
        self.assertIn("Since your last read", self.sent[-1])
        self.assertIn("• Operating Systems: +1 present", self.sent[-1])
        self.click()  # and back, so later tests start from the usual numbers
        self.sent.clear()

    def test_4_leaving_the_page_mid_read_isnt_a_failure(self):
        before = len(self.history())
        FakePortal.course_page_delay = 3
        try:
            self.js(f"(onIconClick(await chrome.tabs.get({self.tab_id()})), null)")  # click, don't wait
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
        self.assertEqual(self.sent, [])  # no failure message, and no summary

    def test_5_a_failed_read_says_why_and_the_next_click_works(self):
        FakePortal.broken_dashboard = True
        try:
            entry = self.click()
        finally:
            FakePortal.broken_dashboard = False
        self.assertFalse(entry["ok"])
        self.assertIn("isn't the dashboard", entry["message"])
        self.assertEqual(self.badge(), "!")
        self.assertTrue(any("Couldn't read your attendance" in text for text in self.sent), self.sent)
        self.assertTrue(self.click()["ok"])

    def test_6_a_double_click_reads_once(self):
        before = len(self.history())
        tab_id = self.tab_id()
        self.js(f"chrome.tabs.get({tab_id}).then((t) => Promise.all([onIconClick(t), onIconClick(t)]))")
        self.assertEqual(sum(1 for path in self.portal_gets() if "task=dashboard" in path), 1)
        self.assertEqual(len(self.history()), before + 1)

    def test_7_telegram_down_shows_on_the_icon(self):
        with mock.patch.object(bot, "send", side_effect=ConnectionError("Telegram is down")):
            entry = self.click()
        self.assertFalse(entry["ok"])
        self.assertIn("couldn't message Telegram", entry["message"])
        self.assertEqual(self.badge(), "!")

    def test_8_clicks_before_login_or_off_this_terms_portal_read_nothing(self):
        before = len(self.history())
        origin = attendance.BASE.rsplit("/newparents/", 1)[0]
        page = self.browser.new_page()
        try:
            page.goto(attendance.LOGIN_URL)
            self.assertIn("Log in to the portal first", self.click(attendance.LOGIN_URL, exact=True)["message"])
            # Off this term's portal, a click opens the settings instead
            for url in (origin + "/webfiles/", origin + "/newparentseven/index.php?ksign=x"):
                page.goto(url)
                self.js(f"onIconClick(await chrome.tabs.get({self.tab_id(url.rsplit('/', 2)[-2])}))")
                for _ in range(30):  # the settings tab appears a moment after the click returns
                    options = self.js("(await chrome.tabs.query({}))"
                                      ".filter((t) => (t.url || t.pendingUrl || '').endsWith('/options.html')).map((t) => t.id)")
                    if options:
                        break
                    time.sleep(0.1)
                self.assertEqual(len(options), 1, url)
                self.js(f"chrome.tabs.remove({options[0]})")
            # A ksign address that shows the login page, as it does once the session has ended
            self.browser.clear_cookies()
            page.goto(attendance.BASE + "index.php?ksign=abc")
            self.assertIn("Log in to the portal first", self.click("ksign=abc")["message"])
            self.assertEqual(self.badge("ksign=abc"), "?")
            self.assertEqual(self.portal_gets(), [])
            self.assertEqual(len(self.history()), before + 2)
        finally:
            page.close()

    def test_9_only_reads_and_forgets_what_older_versions_kept(self):
        # The portal refuses logins done by an extension, and is built to keep automation out: this
        # one never fills in, clicks or opens anything, and has no way to act without a click
        source = (EXTENSION / "background.js").read_text()
        for forbidden in (".click(", ".submit(", "dispatchEvent", "tabs.create", "tabs.remove", "tabs.update",
                          ".value =", ".focus(", "outerHTML", "webNavigation", "alarms", "tabs.onUpdated"):
            self.assertNotIn(forbidden, source)
        manifest = (EXTENSION / "manifest.json").read_text()
        self.assertIn('"permissions": ["scripting", "storage"]', manifest)
        self.js("chrome.storage.local.set({usn: 'x', dob: 'y', last4: 'z', runs: [1], auto: false,"
                " autoRead: false, paused: 'x'})")
        self.js("forgetOldSettings()")
        stored = self.js("chrome.storage.local.get()")
        for key in ("usn", "dob", "last4", "runs", "auto", "autoRead", "paused"):
            self.assertNotIn(key, stored)
        self.assertEqual(stored["token"], TOKEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
