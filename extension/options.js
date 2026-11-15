const $ = (id) => document.getElementById(id);

function showStatus(ok, text) {
  $("status").className = ok === null ? "" : ok ? "ok" : "bad";
  $("status").textContent = text;
}

function showHistory(history = []) {
  $("none").hidden = history.length > 0;
  $("history").replaceChildren(...history.map((h) => {
    const item = document.createElement("li");
    item.className = h.ok ? "ok" : "bad";
    item.textContent = `${new Date(h.at).toLocaleString()}, ${h.mode}: ${h.message}`;
    return item;
  }));
}

$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  await chrome.storage.local.set({token: $("token").value.trim()});
  showStatus(true, "Saved.");
});

function showPaused(paused) {
  $("paused").hidden = !paused;
  if (paused) $("paused-at").textContent = new Date(paused).toLocaleString();
}

$("auto").addEventListener("change", async () => {
  await chrome.storage.local.set({autoRead: $("auto").checked});
  if ($("auto").checked) await chrome.storage.local.remove("paused"); // ticking it is a fresh start
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.history) showHistory(changes.history.newValue);
  if (changes.autoRead) $("auto").checked = changes.autoRead.newValue !== false;
  if (changes.paused) showPaused(changes.paused.newValue);
});

chrome.storage.local.get().then((s) => {
  $("token").value = s.token || "";
  $("auto").checked = s.autoRead !== false; // on unless you turned it off
  showPaused(s.paused);
  showHistory(s.history);
});
