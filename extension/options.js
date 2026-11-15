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
    item.textContent = `${new Date(h.at).toLocaleString()}: ${h.message}`;
    return item;
  }));
}

$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  await chrome.storage.local.set({token: $("token").value.trim()});
  showStatus(true, "Saved.");
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.history) showHistory(changes.history.newValue);
});

chrome.storage.local.get().then((s) => {
  $("token").value = s.token || "";
  showHistory(s.history);
});
