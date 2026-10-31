const FIELDS = ["token", "usn", "dob", "last4"];
const $ = (id) => document.getElementById(id);

function showStatus(ok, text) {
  $("status").className = ok === null ? "" : ok ? "ok" : "bad";
  $("status").textContent = text;
}

function showAuto(auto) {
  $("auto").checked = Boolean(auto);
  $("auto").disabled = auto === undefined; // the first check runs in front of you
  $("auto-hint").hidden = auto !== undefined;
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
  await chrome.storage.local.set(Object.fromEntries(FIELDS.map((f) => [f, $(f).value.trim()])));
  showStatus(true, "Saved.");
});

$("auto").addEventListener("change", () => chrome.storage.local.set({auto: $("auto").checked}));

$("run").addEventListener("click", async () => {
  $("run").disabled = true;
  showStatus(null, "Checking the portal… (up to 3 minutes)");
  try {
    const result = await chrome.runtime.sendMessage("run-now");
    showStatus(result.ok, result.message);
  } catch (error) {
    showStatus(false, `The check stopped unexpectedly: ${error.message}`);
  }
  $("run").disabled = false;
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.history) showHistory(changes.history.newValue);
  if (changes.auto) showAuto(changes.auto.newValue);
});

chrome.storage.local.get().then((s) => {
  for (const f of FIELDS) $(f).value = s[f] || "";
  showAuto(s.auto);
  showHistory(s.history);
});
