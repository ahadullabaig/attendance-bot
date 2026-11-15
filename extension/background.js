// Attendance bot fetcher. Click its icon while you're logged in to the MSRIT parents portal: it
// reads your attendance pages from that session and sends them to the Telegram bot, which does
// the math and sends you the summary, with what changed since your last read.
//
// Why only on a click: since 14 November 2026 the portal answers a login filled in by an extension
// with "Login not allowed: this portal cannot be used from third-party apps", and it added a typed
// security code the next day. It's built to keep automation out, so logging in is yours, and so is
// deciding when to read. This file never fills in, clicks or submits anything on the portal, never
// opens or closes a tab, and does nothing until you click.
//
// Why from your browser at all: since October 2026 the portal's firewall bans IPs that look like
// bots, and it banned the bot's server. Reads come from your own Chrome, in the session you opened,
// at the pace of someone clicking through.

const BOT = "https://attendance-bot-6767.fly.dev";
// Only this term's portal. The term chooser (/webfiles/) and the other terms' portals share the host.
const PORTAL = "https://parents.msrit.edu/newparents/";
const RUN_TIMEOUT = 3 * 60_000;
const PAGE_GAP = 1500; // between course pages, like someone clicking through them

// One read at a time, so a double click can't start two. Checked and set with no await in between.
let busy = false;

chrome.runtime.onInstalled.addListener(forgetOldSettings);
chrome.action.onClicked.addListener(onIconClick);

function forgetOldSettings() {
  // 1.x logged in by itself and kept your portal login; 2.0 read on every page load. Neither's
  // settings mean anything now.
  return chrome.storage.local.remove(["usn", "dob", "last4", "runs", "auto", "autoRead", "paused"]);
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

async function onIconClick(tab) {
  if (!tab.url || !tab.url.startsWith(PORTAL)) return chrome.runtime.openOptionsPage();
  if (busy) return;
  busy = true;
  try {
    const {token} = await chrome.storage.local.get("token");
    if (!token) return chrome.runtime.openOptionsPage();
    if ((await inTab(tab.id, whichPage).catch(() => "gone")) !== "in") {
      badge(tab.id, "?", "Log in to the portal first, then click again");
      return note(false, "Log in to the portal first: this tab isn't past the login yet.");
    }
    let portal;
    try {
      // From the bot, which checks the token before anything goes to the portal
      portal = await callBot(token, "/portal");
    } catch (error) {
      badge(tab.id, "!", error.message);
      return note(false, `Couldn't reach the bot: ${error.message}`);
    }
    await read(token, portal, tab.id);
  } finally {
    busy = false;
  }
}

async function read(token, portal, tabId) {
  // Extension workers stop after 30 s without activity, and reading ten course pages takes about 15
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(), 20_000);
  badge(tabId, "…", "Reading your attendance: stay on this page for a few seconds");
  try {
    const reply = await withTimeout(collect(tabId, token, portal), RUN_TIMEOUT,
      `The read took over ${RUN_TIMEOUT / 60_000} minutes`);
    if (reply.sent === false) {
      badge(tabId, "!", "Read, but the bot couldn't message Telegram");
      return note(false, `Sent attendance for ${reply.courses} courses to the bot, but it couldn't message`
        + " Telegram. /attendance shows the numbers, and the next read reports the changes.");
    }
    badge(tabId, "✓", `Sent attendance for ${reply.courses} courses; the summary is in Telegram`);
    return note(true, `Sent attendance for ${reply.courses} courses; the summary is in Telegram.`);
  } catch (error) {
    if (error.interrupted) {
      // You moved on or closed the tab before it finished: nothing went wrong
      return note(false, "Stopped: the tab left the page before the read finished. Click again to read.");
    }
    badge(tabId, "!", error.message);
    // The message only: nothing from the page you're on goes to the bot
    if (!error.reported) await callBot(token, "/failed", {error: error.message}).catch(() => {});
    return note(false, error.message);
  } finally {
    clearInterval(keepAlive);
  }
}

async function collect(tabId, token, portal) {
  const first = await inTab(tabId, fetchPages, [[portal.dashboard_url], portal.referrer, 0]);
  const dashboard = first.pages[portal.dashboard_url];
  let reply = await callBot(token, "/push", {dashboard});
  if (reply.need) {
    const rest = await inTab(tabId, fetchPages, [reply.need, first.lastUrl, PAGE_GAP]);
    reply = await callBot(token, "/push", {dashboard, pages: rest.pages});
  }
  if (!reply.done) {
    const error = new Error(reply.error || "The bot couldn't read the pages");
    error.reported = Boolean(reply.error); // the bot has already recorded it and told you
    throw error;
  }
  return reply;
}

async function note(ok, message) {
  const {history = []} = await chrome.storage.local.get("history");
  await chrome.storage.local.set({history: [{at: new Date().toISOString(), ok, message}, ...history].slice(0, 20)});
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
