// Attendance bot fetcher. After you log in to the MSRIT parents portal yourself, it reads your
// attendance pages from that session and sends them to the Telegram bot, which does the math.
//
// Why it never logs in: since 14 November 2026 the portal answers a login filled in by an
// extension with "Login not allowed: this portal cannot be used from third-party apps", and it
// added a typed security code the next day. Logging in is yours to do. This file never fills in,
// clicks or submits anything on the portal, and never opens or closes a portal tab.
//
// Why from your browser at all: since October 2026 the portal's firewall bans IPs that look like
// bots, and it banned the bot's server. The reads come from your own Chrome, in the session you
// opened, at the pace of someone clicking through, once a day.

const BOT = "https://attendance-bot-6767.fly.dev";
// Only this term's portal. The term chooser (/webfiles/) and the other terms' portals share the host.
const PORTAL = "https://parents.msrit.edu/newparents/";
const RUN_TIMEOUT = 3 * 60_000;
const PAGE_GAP = 1500; // between course pages, like someone clicking through them
const SETTLE = 1000; // a page may still redirect by script; only the page you end up on is read

// One read at a time. Checked and set with no await in between, so two page loads close together
// (a redirect, two portal tabs) can't both start one. A worker that restarts starts idle, which is
// right: its read died with it.
let busy = false;

chrome.runtime.onInstalled.addListener(forgetPortalLogin);
chrome.action.onClicked.addListener(onIconClick);
chrome.webNavigation.onCompleted.addListener(onPortalPage, {url: [{urlPrefix: PORTAL}]});

function forgetPortalLogin() {
  // Versions before 2.0 logged in themselves and kept your portal login; it isn't needed any more.
  // Their "auto" switch went too: it meant "log in by yourself", which you may have turned off.
  return chrome.storage.local.remove(["usn", "dob", "last4", "runs", "auto"]);
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

function badge(tabId, text, title) {
  // Tab-specific, so it clears by itself when the tab moves on to another page
  chrome.action.setBadgeText({tabId, text}).catch(() => {});
  chrome.action.setTitle({tabId, title}).catch(() => {});
}

async function onPortalPage(details) {
  // Pages of the portal after logging in all go through index.php?ksign=… (the encrypted address)
  if (details.frameId !== 0 || !new URL(details.url).searchParams.has("ksign")) return;
  await new Promise((resolve) => setTimeout(resolve, SETTLE));
  const tab = await chrome.tabs.get(details.tabId).catch(() => null);
  if (!tab || tab.url !== details.url || busy) return; // moved on, or a read is already going
  busy = true;
  try {
    const s = await chrome.storage.local.get();
    if (!s.token || s.autoRead === false || s.paused) return;
    let due;
    try {
      due = await callBot(s.token, "/due");
    } catch {
      return; // offline, or the bot is restarting: the next portal page asks again
    }
    if (!due.read) return;
    if ((await inTab(details.tabId, whichPage).catch(() => "gone")) !== "in") return;
    await read(s, due, details.tabId, "auto");
  } finally {
    busy = false;
  }
}

async function onIconClick(tab) {
  if (!tab.url || !tab.url.startsWith(PORTAL)) return chrome.runtime.openOptionsPage();
  if (busy) return;
  busy = true;
  try {
    const s = await chrome.storage.local.get();
    if (!s.token) return chrome.runtime.openOptionsPage();
    const page = await inTab(tab.id, whichPage).catch(() => "gone");
    if (page !== "in") {
      badge(tab.id, "?", "Log in to the portal first, then click again");
      return note("manual", false, "Log in to the portal first: this tab isn't past the login yet.");
    }
    let due;
    try {
      due = await callBot(s.token, "/due");
    } catch (error) {
      return note("manual", false, `Couldn't reach the bot: ${error.message}`);
    }
    await read(s, due, tab.id, "manual");
  } finally {
    busy = false;
  }
}

async function read(s, due, tabId, mode) {
  // Extension workers stop after 30 s without activity, and reading ten course pages takes about 15
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(), 20_000);
  badge(tabId, "…", "Reading your attendance: stay on this page for a few seconds");
  try {
    const courses = await withTimeout(collect(tabId, s.token, due, mode), RUN_TIMEOUT,
      `The read took over ${RUN_TIMEOUT / 60_000} minutes`);
    badge(tabId, "✓", `Sent attendance for ${courses} courses to the bot`);
    await chrome.storage.local.remove("paused"); // it works again, so automatic reads resume
    return note(mode, true, `Sent attendance for ${courses} courses to the bot.`);
  } catch (error) {
    if (error.interrupted) {
      // You moved on or closed the tab: nothing went wrong, and the next portal page tries again
      return note(mode, false, "Stopped: the tab left the page before the read finished.");
    }
    badge(tabId, "!", error.message);
    if (!error.reported) await callBot(s.token, "/failed", {error: error.message}).catch(() => {});
    if (mode === "auto") {
      // Something about the portal is off; don't try again on every page you open. Kept apart from
      // your own on/off switch, and cleared by the next read that works.
      await chrome.storage.local.set({paused: new Date().toISOString()});
      return note(mode, false, `${error.message} Automatic reads are paused until a read works:`
        + " click the icon on a portal page to try again.");
    }
    return note(mode, false, error.message);
  } finally {
    clearInterval(keepAlive);
  }
}

async function collect(tabId, token, due, mode) {
  const first = await inTab(tabId, fetchPages, [[due.dashboard_url], due.referrer, 0]);
  const dashboard = first.pages[due.dashboard_url];
  // A click on the icon asks for the numbers now, so the bot sends the summary straight away
  const manual = mode === "manual";
  let reply = await callBot(token, "/push", {dashboard, manual});
  if (reply.need) {
    const rest = await inTab(tabId, fetchPages, [reply.need, first.lastUrl, PAGE_GAP]);
    reply = await callBot(token, "/push", {dashboard, pages: rest.pages, manual});
  }
  if (!reply.done) {
    const error = new Error(reply.error || "The bot couldn't read the pages");
    error.reported = Boolean(reply.error); // the bot has already recorded it and told you
    throw error;
  }
  return reply.courses;
}

async function note(mode, ok, message) {
  const {history = []} = await chrome.storage.local.get("history");
  const entry = {at: new Date().toISOString(), mode, ok, message};
  await chrome.storage.local.set({history: [entry, ...history].slice(0, 20)});
  return {ok, message};
}

function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function inTab(tabId, func, args = []) {
  let injection;
  try {
    [injection] = await chrome.scripting.executeScript({target: {tabId}, world: "MAIN", func, args});
  } catch (error) {
    throw Object.assign(new Error(`The tab went away: ${error.message}`), {interrupted: true});
  }
  const result = injection && injection.result;
  // A page that unloads while the function runs gives no result at all
  if (result === undefined || result === null) throw Object.assign(new Error("The tab left the page"), {interrupted: true});
  if (result.error) throw new Error(result.error);
  return result;
}

// The functions below run inside the portal tab (world: MAIN, so fetch() goes out as the page's
// own request). They can't use anything from this file, and they report errors as {error}
// because an exception thrown in the page doesn't reach the extension. They only read.

function whichPage() {
  const shown = (el) => el.getClientRects().length > 0;
  if (document.querySelector("input#username")) return "login";
  const select = [...document.querySelectorAll("select")].find(shown);
  if (select && [...select.options].some((o) => /father/i.test(o.text))) return "verify";
  return "in";
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
