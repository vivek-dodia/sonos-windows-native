const $ = (id) => document.getElementById(id);

function render(r) {
  const p = r.prefs;
  $("enabled").checked = p.enabled;
  $("offset").value = p.offset;
  $("offsetText").textContent = (p.offset > 0 ? "+" : "") + p.offset + " ms";
  const dot = $("dot");
  dot.className = "dot";
  if (r.state === "not-running") {
    $("title").textContent = "Sonos Cast isn't running";
    $("detail").textContent = "Start it from the Start Menu (Sonos Cast).";
  } else if (r.sonos == null) {
    $("title").textContent = "Not casting";
    $("detail").textContent = "Video plays normally. Play something and it'll sync once casting starts.";
  } else {
    dot.classList.add(p.enabled ? "ok" : "warn");
    $("title").textContent = p.enabled ? "Video synced to the Sonos" : "Sync off";
    $("detail").innerHTML = p.enabled
      ? `<span class="big">${r.delay} ms</span> <span class="muted">picture held back</span><br>` +
        `<span class="muted">Sonos sound arrives ${r.sonos} ms late.</span>`
      : `<span class="muted">Sonos sound is ${r.sonos} ms behind the picture.</span>`;
  }
}

function renderTab(t) {
  const el = $("tab");
  if (!t || !t.seen) { el.textContent = ""; return; }
  el.textContent = t.syncing ? "This tab: video synced." :
    t.drm ? "This tab: DRM-protected video - Chrome won't let it be synced. Sound trails the picture slightly." :
    "This tab: no audible video playing.";
}

function refresh() {
  chrome.runtime.sendMessage({ type: "sync" }, render);
  chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
    if (tab) chrome.runtime.sendMessage({ type: "tab", tabId: tab.id }, renderTab);
  });
}

$("enabled").onchange = (e) => chrome.storage.sync.set({ enabled: e.target.checked }, refresh);
$("offset").oninput = (e) => chrome.storage.sync.set({ offset: Number(e.target.value) }, refresh);
$("zero").onclick = () => chrome.storage.sync.set({ offset: 0 }, refresh);
refresh();
setInterval(refresh, 1000);
