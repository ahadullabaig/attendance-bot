// Attendance bot fetcher. Once a day it logs in to the MSRIT parents portal in this browser and
// sends the attendance pages to the Telegram bot, which reads them and does the math.
//
// Why from here: since its October 2026 rebuild, the portal's firewall bans IPs that look like
// bots, and it banned the bot's server. These requests come from your own Chrome on your own
// connection, at the pace of someone clicking through, a few times a day at most. The bot decides
// when a check is due and reads the pages; this file only does what a person would do in a tab.

const BOT = "https://attendance-bot-6767.fly.dev";
const TICK_MINUTES = 2; // how often to ask the bot whether a check is due
const MIN_GAP = 10 * 60_000; // automatic runs never start closer together than this,
const MAX_RUNS_PER_DAY = 4; // or more often than this, whatever the bot says
const RUN_TIMEOUT = 3 * 60_000;
const PAGE_GAP = 1500; // between course pages, like someone clicking through them
// A manual run's tab is in front of you, so there's time to solve a captcha puzzle if Google shows one
const CAPTCHA_WAIT = {auto: 30_000, manual: 120_000};
const DAY = 24 * 3600_000;

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "tick") return tick();
});
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message === "run-now") {
    runNow().then(reply);
    return true; // the reply comes later
  }
});

function schedule() {
  chrome.alarms.create("tick", {delayInMinutes: 1, periodInMinutes: TICK_MINUTES});
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function ready(s) {
  return Boolean(s.token && s.usn && s.dob && s.last4);
}

async function callBot(token, path, body) {
  const response = await fetch(BOT + path, {
    method: body ? "POST" : "GET",
    headers: {Authorization: `Bearer ${token}`, ...(body && {"Content-Type": "application/json"})},
    body: body && JSON.stringify(body),
  });
  if (response.status === 401) throw new Error("The bot didn't accept the token");
  if (!response.ok) throw new Error(`The bot answered HTTP ${response.status}`);
  return response.json();
}

async function portalOpen(due) {
  // The portal allows one session per student, so logging in here would log you out in that tab
  const tabs = await chrome.tabs.query({url: new URL(due.login_url).origin + "/*"});
  return tabs.length > 0;
}

async function running() {
  const {running} = await chrome.storage.session.get("running");
  if (running && Date.now() - running.at > RUN_TIMEOUT + 60_000) {
    // A run whose worker was stopped midway: close its tab, or it would block every later run
    await chrome.tabs.remove(running.tabId).catch(() => {});
    await chrome.storage.session.remove("running");
    return false;
  }
  return Boolean(running);
}

async function tick() {
  const s = await chrome.storage.local.get();
  if (!s.auto || !ready(s)) return; // automatic checks start after a successful Run now, which you watch
  if (await running()) return;
  let due;
  try {
    due = await callBot(s.token, "/due");
  } catch {
    return; // offline, or the bot is restarting: ask again next tick
  }
  if (!due.fetch) return;
  const runs = (s.runs || []).filter((t) => Date.now() - t < DAY);
  if (runs.length >= MAX_RUNS_PER_DAY || Date.now() - Math.max(0, ...runs) < MIN_GAP) return;
  if (await portalOpen(due)) return; // you're using it: try again next tick
  await run(s, due, "auto");
}

async function runNow() {
  const s = await chrome.storage.local.get();
  if (!ready(s)) return {ok: false, message: "Fill in all four settings and save them first."};
  if (await running()) return {ok: false, message: "A check is already running."};
  let due;
  try {
    due = await callBot(s.token, "/due");
  } catch (error) {
    return {ok: false, message: `Couldn't reach the bot: ${error.message}`};
  }
  if (await portalOpen(due)) {
    return {ok: false, message: "The portal is open in a tab. Close it first: the portal allows one login at a time."};
  }
  const result = await run(s, due, "manual");
  if (result.ok && s.auto === undefined) await chrome.storage.local.set({auto: true}); // first success
  return result;
}

async function run(s, due, mode) {
  await chrome.storage.session.set({running: {at: Date.now(), tabId: null}});
  await chrome.storage.local.set({runs: [...(s.runs || []).filter((t) => Date.now() - t < DAY), Date.now()]});

  // Extension workers stop after 30 s without activity, and a captcha can take longer than that
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(), 20_000);
  const nav = {tabId: null, error: null};
  const onError = (details) => {
    // ERR_ABORTED is a navigation replaced by the next one, e.g. after clicking Login
    if (details.tabId === nav.tabId && details.frameId === 0 && details.error !== "net::ERR_ABORTED") {
      nav.error = details.error;
    }
  };
  chrome.webNavigation.onErrorOccurred.addListener(onError);
  try {
    const tab = await chrome.tabs.create({url: due.login_url, active: mode === "manual"});
    nav.tabId = tab.id;
    await chrome.storage.session.set({running: {at: Date.now(), tabId: tab.id}});
    try {
      const courses = await withTimeout(drive(nav, s, due, CAPTCHA_WAIT[mode]), RUN_TIMEOUT,
        "The check took over 3 minutes");
      return note(mode, true, `Sent attendance for ${courses} courses to the bot.`);
    } catch (error) {
      if (!error.reported) await report(s.token, nav, error.message);
      return note(mode, false, error.message);
    } finally {
      await chrome.tabs.remove(tab.id).catch(() => {});
    }
  } catch (error) {
    return note(mode, false, `Couldn't open a tab: ${error.message}`); // e.g. no Chrome window open
  } finally {
    clearInterval(keepAlive);
    chrome.webNavigation.onErrorOccurred.removeListener(onError);
    await chrome.storage.session.remove("running");
  }
}

async function drive(nav, s, due, captchaWait) {
  await firstLoad(nav);
  const seen = new Set();
  for (let step = 0; step < 4; step++) {
    await sleep(1000); // let any redirect by script finish before looking at the page
    const page = await inTab(nav, probe);
    if (page === "other") return collect(nav, s.token, due);
    if (seen.has(page)) {
      throw new Error(page === "login"
        ? "The portal showed the login page again after logging in. Check the USN and date of birth."
        : "The verification page came back. Check the last 4 digits of your father's mobile.");
    }
    seen.add(page);
    const next = nextLoad(nav); // listening before the click, so the new page can't slip past
    try {
      if (page === "login") await inTab(nav, fillLogin, [s.usn, s.dob, captchaWait]);
      else await inTab(nav, fillVerification, [s.last4]);
      // The clock starts after filling in, which includes waiting for the captcha
      await withTimeout(next.loaded, 30_000, `Nothing happened after submitting the ${page} page`);
    } finally {
      next.stop();
    }
  }
  throw new Error("Didn't reach the dashboard");
}

async function collect(nav, token, due) {
  const first = await inTab(nav, fetchPages, [[due.dashboard_url], due.login_url, 0]);
  const dashboard = first.pages[due.dashboard_url];
  let reply = await callBot(token, "/push", {dashboard});
  if (reply.need) {
    const rest = await inTab(nav, fetchPages, [reply.need, first.lastUrl, PAGE_GAP]);
    reply = await callBot(token, "/push", {dashboard, pages: rest.pages});
  }
  if (!reply.done) {
    const error = new Error(reply.error || "The bot couldn't read the pages");
    error.reported = Boolean(reply.error); // the bot has already recorded it and told you
    throw error;
  }
  return reply.courses;
}

async function report(token, nav, message) {
  let page = {};
  try {
    page = await inTab(nav, () => ({url: location.href, html: document.documentElement.outerHTML}));
  } catch {
    // an error page or a closed tab: send the message alone
  }
  await callBot(token, "/failed", {error: message, ...page}).catch(() => {});
}

async function note(mode, ok, message) {
  const {history = []} = await chrome.storage.local.get("history");
  const entry = {at: new Date().toISOString(), mode, ok, message};
  await chrome.storage.local.set({history: [entry, ...history].slice(0, 20)});
  return {ok, message};
}

function navError(nav) {
  const refused = nav.error === "net::ERR_CONNECTION_REFUSED";
  return new Error(`The portal didn't load (${nav.error}).` + (refused
    ? " It may be down, or it may have blocked your connection. If it opens on your phone's mobile"
      + " data, it's a block: turn off automatic checks and talk to college IT instead of retrying."
    : ""));
}

async function firstLoad(nav) {
  for (const until = Date.now() + 30_000; Date.now() < until; await sleep(300)) {
    if (nav.error) throw navError(nav);
    const tab = await chrome.tabs.get(nav.tabId);
    if (tab.status === "complete" && tab.url && tab.url !== "about:blank") {
      if (nav.error) throw navError(nav);
      return;
    }
  }
  throw new Error("The portal didn't load within 30 seconds");
}

function nextLoad(nav) {
  let listener;
  const loaded = new Promise((resolve, reject) => {
    listener = (tabId, info) => {
      if (tabId === nav.tabId && info.status === "complete") {
        if (nav.error) reject(navError(nav));
        else resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  loaded.catch(() => {}); // awaited by the caller; this only stops a stray unhandled rejection
  return {loaded, stop: () => chrome.tabs.onUpdated.removeListener(listener)};
}

function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function inTab(nav, func, args = []) {
  let injection;
  try {
    [injection] = await chrome.scripting.executeScript({target: {tabId: nav.tabId}, world: "MAIN", func, args});
  } catch (error) {
    if (nav.error) throw navError(nav);
    throw new Error(`Couldn't read the portal tab: ${error.message}`);
  }
  const result = injection && injection.result;
  if (result && result.error) throw new Error(result.error);
  return result;
}

// The functions below run inside the portal tab (world: MAIN, so they can call the page's own
// putdate()). They can't use anything from this file, and they report errors as {error} because
// an exception thrown in the page doesn't reach the extension.

function probe() {
  const shown = (el) => el.getClientRects().length > 0;
  if (document.querySelector('input#username') && document.querySelector('input[type=submit][value="Login"]')) {
    return "login";
  }
  const select = [...document.querySelectorAll("select")].find(shown);
  if (select && [...select.options].some((o) => /father/i.test(o.text))) return "verify";
  return "other";
}

async function fillLogin(usn, dob, captchaWait) {
  const set = (el, value) => {
    el.value = value;
    for (const type of ["input", "change"]) el.dispatchEvent(new Event(type, {bubbles: true}));
  };
  const pick = (id, matches) => {
    const select = document.getElementById(id);
    const option = select && [...select.options].find(matches);
    if (!option) throw new Error(`The login page has no matching choice in its #${id} list`);
    set(select, option.value);
  };
  try {
    const [yyyy, mm, dd] = dob.split("-");
    set(document.getElementById("username"), usn);
    // The day options' values carry a trailing space ('16 '), so match the visible label
    pick("dd", (o) => o.text.trim() === dd || o.text.trim() === String(Number(dd)));
    pick("mm", (o) => o.value === mm);
    pick("yyyy", (o) => o.value === yyyy);
    if (typeof putdate === "function") putdate(); // the page builds its hidden password field from the lists
    // The invisible reCAPTCHA runs on page load and fills this field when it passes
    const captcha = document.getElementById("captcha-response");
    if (!captcha) throw new Error("The login page has no captcha field; the portal may have been redesigned");
    for (const until = Date.now() + captchaWait; !captcha.value;) {
      if (Date.now() > until) throw new Error("reCAPTCHA didn't pass. Try Run now, which shows the tab.");
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    const button = document.querySelector('input[type=submit][value="Login"]');
    if (!button) throw new Error("The login page has no Login button");
    setTimeout(() => button.click(), 100); // after this has returned, since the click leaves the page
    return null;
  } catch (error) {
    return {error: error.message};
  }
}

function fillVerification(last4) {
  const shown = (el) => el.getClientRects().length > 0;
  const fire = (el, ...types) => types.forEach((type) => el.dispatchEvent(new Event(type, {bubbles: true})));
  const type = (box, text) => {
    box.focus();
    box.value = "";
    for (const key of text) {
      box.dispatchEvent(new KeyboardEvent("keydown", {key, bubbles: true}));
      box.value += key;
      box.dispatchEvent(new InputEvent("input", {data: key, inputType: "insertText", bubbles: true}));
      box.dispatchEvent(new KeyboardEvent("keyup", {key, bubbles: true}));
    }
    fire(box, "change");
  };
  try {
    const select = [...document.querySelectorAll("select")].find(shown);
    const father = select && [...select.options].find((o) => /father/i.test(o.text));
    if (!father) throw new Error("The verification page has no 'Father Mobile' choice");
    select.value = father.value;
    fire(select, "input", "change");
    const form = select.closest("form");
    if (!form) throw new Error("The verification page has no form around the list");
    const boxes = [...form.querySelectorAll("input")]
      .filter((input) => shown(input) && !["submit", "button", "hidden"].includes(input.type));
    if (boxes.length === 4) boxes.forEach((box, i) => type(box, last4[i]));
    else if (boxes.length) type(boxes[0], last4);
    else throw new Error("The verification page has no box for the digits");
    const button = [...form.querySelectorAll("button, input[type=submit], input[type=button]")]
      .find((b) => /submit/i.test(`${b.textContent} ${b.value}`));
    if (!button) throw new Error("The verification page has no Submit button");
    setTimeout(() => button.click(), 100);
    return null;
  } catch (error) {
    return {error: error.message};
  }
}

async function fetchPages(urls, referrer, gap) {
  const pages = {};
  let lastUrl = null;
  try {
    for (const [i, url] of urls.entries()) {
      if (i) await new Promise((resolve) => setTimeout(resolve, gap));
      // Every request needs a Referer from the portal itself, or it logs the session out
      const response = await fetch(url, {
        referrer, referrerPolicy: "same-origin", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) return {error: `The portal answered HTTP ${response.status} for a page`};
      pages[url] = await response.text();
      lastUrl = response.url;
    }
  } catch (error) {
    return {error: `Couldn't load a portal page: ${error.message}`};
  }
  return {pages, lastUrl};
}
