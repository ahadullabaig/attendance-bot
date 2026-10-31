#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.47", "beautifulsoup4", "requests"]
# ///
"""End-to-end test of the Chrome extension: the real extension in Chromium, the bot's real web
server, and a fake portal that behaves like the real one (invisible captcha, verification step,
Referer rule). Nothing here reaches the real portal or Telegram.

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

    def login_page(self, headers=()):
        self.page(LOGIN_PAGE.replace("CAPTCHA_MS", str(self.captcha_ms)), headers=headers)

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
        self.wfile.write(data)

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
        self.page(DASHBOARD if query["task"] == ["dashboard"] else DETAIL)

    def do_POST(self):
        self.requests.append(("POST", self.path, self.headers.get("Referer")))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        if "task=verify" in self.path:
            digits = "".join(form.get(f"d{i}", "") for i in range(1, 5))
            if self.cookies().get("stage") == "verify" and form.get("type") == "F" and digits == LAST4:
                return self.page("", 302, [("Location", "index.php?ksign=abc"), ("Set-Cookie", "session=good")])
            return self.page(VERIFY_PAGE)
        if form.get("username") == USN and form.get("passwd") == DOB and form.get("captcha"):
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
        (ext / "background.js").write_text(script.replace('const BOT = "https://attendance-bot-6767.fly.dev";', local))
        manifest = (ext / "manifest.json").read_text()
        manifest = manifest.replace('"https://parents.msrit.edu/*", "https://attendance-bot-6767.fly.dev/*"',
                                    '"http://127.0.0.1/*"')
        (ext / "manifest.json").write_text(manifest)
        assert local in (ext / "background.js").read_text() and "127.0.0.1" in manifest

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch_persistent_context(
            tmp / "profile", executable_path=CHROMIUM, channel=None if CHROMIUM else "chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        cls.worker = (cls.browser.service_workers or [cls.browser.wait_for_event("serviceworker")])[0]
        cls.js("chrome.storage.local.set(%s)" % {"token": TOKEN, "usn": USN, "dob": DOB, "last4": LAST4})

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

    def portal_tabs(self):
        return self.js("(await chrome.tabs.query({})).filter((t) => (t.url || '').includes('/newparents/')).length")

    def test_1_first_run_by_hand(self):
        self.assertIsNone(self.js("(await chrome.storage.local.get()).auto"))
        self.js("tick()")  # automatic checks stay off until a Run now has worked
        self.assertEqual(FakePortal.requests, [])

        result = self.js("runNow()")
        self.assertEqual(result, {"ok": True, "message": "Sent attendance for 2 courses to the bot."})
        first, second = self.state["data"]["courses"]
        self.assertEqual((first["present"], first["absent"], first["remaining"], first["can_skip"]), (9, 3, 30, 3))
        self.assertNotIn("present", second)
        self.assertIs(self.js("(await chrome.storage.local.get()).auto"), True)
        self.assertEqual(self.portal_tabs(), 0)  # the tab is closed afterwards

        steps = [(method, path.split("?")[-1]) for method, path, _ in FakePortal.requests]
        self.assertEqual(steps[:4], [("GET", "/newparents/index.php"), ("POST", "/newparents/index.php"),
                                     ("POST", "task=verify"), ("GET", "ksign=abc")])
        for method, path, referer in FakePortal.requests:
            if "task=" in path and method == "GET":
                self.assertTrue(referer and referer.startswith(attendance.BASE), (path, referer))

    def test_2_not_due_means_no_portal_visit(self):
        self.js("tick()")
        self.assertEqual(FakePortal.requests, [])

    def test_3_refresh_runs_in_the_background_and_answers(self):
        self.state["refresh_wanted"] = "2026-10-31T10:00:00"
        self.js("chrome.storage.local.set({runs: []})")  # the 10-minute gap since the last run
        self.js("tick()")
        self.assertNotIn("refresh_wanted", self.state)
        self.assertTrue(any("<pre>" in text for text in self.sent), self.sent)
        history = self.js("(await chrome.storage.local.get()).history")
        self.assertEqual((history[0]["mode"], history[0]["ok"]), ("auto", True))

    def test_4_skips_while_you_have_the_portal_open(self):
        page = self.browser.new_page()
        page.goto(attendance.LOGIN_URL)
        FakePortal.requests.clear()
        try:
            result = self.js("runNow()")
        finally:
            page.close()
        self.assertFalse(result["ok"])
        self.assertIn("open in a tab", result["message"])
        self.assertEqual(FakePortal.requests, [])

    def test_5_wrong_details_are_reported_without_retrying(self):
        self.js("chrome.storage.local.set({dob: '2004-05-17'})")
        try:
            result = self.js("runNow()")
        finally:
            self.js(f"chrome.storage.local.set({{dob: '{DOB}'}})")
        self.assertFalse(result["ok"])
        self.assertIn("login page again", result["message"])
        self.assertEqual(sum(1 for m, _, _ in FakePortal.requests if m == "POST"), 1)  # one try, no loop
        self.assertEqual(len(self.state["fetch_failed"]), 1)
        self.assertTrue(any("couldn't check the portal" in text for text in self.sent), self.sent)
        saved = (attendance.DEBUG_DIR / "extension-failed.html").read_text()
        self.assertIn('id="username"', saved)
        for secret in ("2004-05-17", USN, LAST4):  # typed values aren't part of the saved page
            self.assertNotIn(secret, saved)

    def test_6_slow_captcha_on_a_manual_run(self):
        # You might take a while over a puzzle; the run must wait for it, not give up after 30 s
        FakePortal.captcha_ms = 31_000
        try:
            result = self.js("runNow()")
        finally:
            FakePortal.captcha_ms = 800
        self.assertEqual(result, {"ok": True, "message": "Sent attendance for 2 courses to the bot."})

    def test_7_refused_connection_says_so(self):
        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        with mock.patch.object(attendance, "LOGIN_URL", f"http://127.0.0.1:{closed}/newparents/index.php"):
            result = self.js("runNow()")
        self.assertFalse(result["ok"])
        self.assertIn("ERR_CONNECTION_REFUSED", result["message"])
        self.assertEqual(self.portal_tabs(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
