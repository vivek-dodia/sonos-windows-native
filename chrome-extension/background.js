// Asks the Sonos Cast tray app how far the Sonos sound trails the picture.
// Content scripts can't reach 127.0.0.1 from https pages (CORS / private network
// rules); the service worker can, thanks to host_permissions.
// Also keeps the toolbar badge showing what the current tab is doing.

const STATUS_URL = "http://127.0.0.1:8080/status";
let cache = { at: 0, status: null };

async function getStatus() {
  if (Date.now() - cache.at < 1000) return cache.status;
  let status = null;
  try {
    const r = await fetch(STATUS_URL, { cache: "no-store" });
    status = await r.json();
  } catch (e) {
    status = null; // tray app not running
  }
  cache = { at: Date.now(), status };
  return status;
}

// tabId -> Map(frameId -> {syncing, drm, delay})
const tabs = new Map();

function updateBadge(tabId) {
  const frames = [...(tabs.get(tabId) || new Map()).values()];
  const syncing = frames.find((f) => f.syncing);
  let text = "", color = "#10b981", title = "Sonos Sync";
  if (syncing) {
    text = String(syncing.delay);
    title = `Sonos Sync: video held back ${syncing.delay} ms to match the Sonos`;
  } else if (frames.some((f) => f.drm)) {
    text = "DRM";
    color = "#6b7280";
    title = "Sonos Sync: this video is DRM-protected and can't be synced";
  }
  chrome.action.setBadgeText({ tabId, text });
  chrome.action.setBadgeBackgroundColor({ tabId, color });
  chrome.action.setTitle({ tabId, title });
}

chrome.tabs.onRemoved.addListener((tabId) => tabs.delete(tabId));
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading") { tabs.delete(tabId); updateBadge(tabId); }
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "report" && sender.tab) {
    if (!tabs.has(sender.tab.id)) tabs.set(sender.tab.id, new Map());
    tabs.get(sender.tab.id).set(sender.frameId, msg);
    updateBadge(sender.tab.id);
    return;
  }
  if (msg.type === "tab") { // popup asking about a tab
    const frames = [...(tabs.get(msg.tabId) || new Map()).values()];
    reply({ syncing: frames.some((f) => f.syncing), drm: frames.some((f) => f.drm), seen: frames.length > 0 });
    return;
  }
  if (msg.type !== "sync") return;
  Promise.all([getStatus(), chrome.storage.sync.get({ enabled: true, offset: 0 })]).then(([s, prefs]) => {
    const sonos = s && s.av_delay_ms != null ? s.av_delay_ms : null;
    const delay = prefs.enabled && sonos != null ? Math.max(0, sonos + prefs.offset) : 0;
    reply({ delay, sonos, state: s ? s.state : "not-running", prefs });
  });
  return true; // async reply
});
