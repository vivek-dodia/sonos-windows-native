"""Chrome extension test in Playwright's Chromium (branded Chrome no longer allows
--load-extension). Needs the tray app running and casting, plus a test page:

    python tests/test_extension.py <dir with index.html + test.mp4> [youtube-url]

Checks the on-screen picture trails the video by the Sonos delay, the overlay
sits exactly on the video, playback stays smooth, and it gets out of the way
when casting stops.
"""
import functools
import http.server
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

EXT = Path(__file__).resolve().parent.parent / "chrome-extension"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def api(cmd):
    with urllib.request.urlopen(f"http://127.0.0.1:8080/{cmd}", timeout=5) as r:
        return json.loads(r.read())


def serve(root):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SAMPLE = """() => {
  const v = document.querySelector('video');
  const c = v.nextElementSibling;
  const vr = v.getBoundingClientRect(), cr = c && c.tagName === 'CANVAS' ? c.getBoundingClientRect() : null;
  const q = v.getVideoPlaybackQuality();
  return { lag: v.dataset.sonosSyncLag, delay: v.dataset.sonosSyncDelay,
           overlay: !!cr && getComputedStyle(c).display !== 'none',
           aligned: !!cr && Math.abs(vr.x - cr.x) < 1 && Math.abs(vr.y - cr.y) < 1 &&
                    Math.abs(vr.width - cr.width) < 1 && Math.abs(vr.height - cr.height) < 1,
           dropped: q.droppedVideoFrames, total: q.totalVideoFrames, paused: v.paused, t: v.currentTime };
}"""


def wake_display():
    """A sleeping monitor makes Windows render at ~50 Hz and wrecks frame-rate numbers."""
    import ctypes
    ctypes.windll.user32.keybd_event(0x10, 0, 0, 0)
    ctypes.windll.user32.keybd_event(0x10, 0, 2, 0)
    time.sleep(3)


def main(root, youtube=None):
    wake_display()
    status = api("status")
    if status.get("av_delay_ms") is None:
        print("start casting first (tray app) - av_delay_ms is null")
        return False
    expected = status["av_delay_ms"] / 1000
    srv = serve(root)
    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=False, viewport={"width": 1100, "height": 700},
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--autoplay-policy=no-user-gesture-required", "--window-position=-2400,0"])
        page = ctx.new_page()
        page.goto("http://127.0.0.1:8765/index.html")
        # baseline smoothness with the overlay off (this machine may drop frames on its own)
        api("stop")
        time.sleep(4)
        b0 = page.evaluate(SAMPLE)
        time.sleep(5)
        b1 = page.evaluate(SAMPLE)
        base_drop = (b1["dropped"] - b0["dropped"]) / max(1, b1["total"] - b0["total"])
        api("start")
        time.sleep(10)
        status = api("status")
        expected = status["av_delay_ms"] / 1000
        samples = []
        for _ in range(20):
            samples.append(page.evaluate(SAMPLE))
            time.sleep(0.25)
        lags = sorted(float(s["lag"]) for s in samples if s["lag"])
        s = samples[-1]
        check("overlay is active while casting", s["overlay"] and abs(int(s["delay"]) - status["av_delay_ms"]) < 30,
              f"delay attr {s['delay']} ms")
        check("overlay sits exactly on the video", s["aligned"])
        if lags:
            med = lags[len(lags) // 2]
            # lag = currentTime - shown frame's time; one 60 fps frame (17 ms) + timer jitter of slack
            check("picture trails the video by the Sonos delay", abs(med - expected) < 0.035,
                  f"median {med * 1000:.0f} ms vs Sonos delay {expected * 1000:.0f} ms, "
                  f"range {lags[0] * 1000:.0f}..{lags[-1] * 1000:.0f}")
        else:
            check("picture trails the video by the Sonos delay", False, "no lag samples")
        first, last = samples[0], samples[-1]
        drop_rate = (last["dropped"] - first["dropped"]) / max(1, last["total"] - first["total"])
        check("overlay adds no dropped frames", drop_rate < base_drop + 0.02,
              f"{drop_rate:.1%} dropped vs {base_drop:.1%} without overlay")

        page.evaluate("document.querySelector('video').pause()")
        time.sleep(1)
        a = page.evaluate(SAMPLE)
        check("pausing: overlay settles on the paused frame", a["overlay"] and abs(float(a["lag"] or 9)) < 0.04,
              f"lag {a['lag']}")
        page.evaluate("document.querySelector('video').play()")

        page.set_viewport_size({"width": 800, "height": 600})
        page.evaluate("document.querySelector('div').style.width='600px'; document.querySelector('div').style.height='338px'")
        time.sleep(1)
        check("overlay follows a resized player", page.evaluate(SAMPLE)["aligned"])

        muted = page.evaluate("""() => { const v = document.getElementById('preview');
            const c = v.nextElementSibling;
            return { lag: v.dataset.sonosSyncLag || null,
                     overlay: !!c && c.tagName === 'CANVAS' && getComputedStyle(c).display !== 'none' }; }""")
        check("muted video (e.g. hover preview) is left alone - no sound to sync", not muted["overlay"] and muted["lag"] is None)

        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        badge = sw.evaluate("chrome.tabs.query({active: true}).then(t => chrome.action.getBadgeText({tabId: t[0].id}))")
        check("toolbar badge shows the delay", badge.isdigit() and abs(int(badge) - status["av_delay_ms"]) < 40, f"badge {badge!r}")
        ext_id = sw.url.split("/")[2]
        popup = ctx.new_page()
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
        time.sleep(1.5)
        text = popup.inner_text("body")
        check("popup shows synced status and the delay", "Video synced" in text and "ms" in text, text.splitlines()[0])
        popup.close()
        page.bring_to_front()

        api("stop")
        time.sleep(3.5)
        s = page.evaluate(SAMPLE)
        check("not casting -> overlay gone, plain video", not s["overlay"] and s["delay"] == "0")
        time.sleep(1)
        loop = page.evaluate("document.documentElement.dataset.sonosSyncLoop")
        badge = sw.evaluate("chrome.tabs.query({active: true}).then(t => chrome.action.getBadgeText({tabId: t[0].id}))")
        check("not casting -> frame loop stopped, badge cleared (no cost)", loop == "off" and badge == "", f"loop {loop}, badge {badge!r}")
        api("start")
        time.sleep(8)
        s = page.evaluate(SAMPLE)
        check("casting again -> overlay back", s["overlay"])

        if youtube:
            page.goto(youtube)
            time.sleep(12)
            try:
                page.evaluate("document.querySelector('video').play()")
            except Exception:
                pass
            time.sleep(4)
            ys = []
            for _ in range(12):
                ys.append(page.evaluate(SAMPLE))
                time.sleep(0.25)
            y = ys[-1]
            ylags = sorted(float(s["lag"]) for s in ys if s["lag"])
            ymed = ylags[len(ylags) // 2] if ylags else None
            target = int(y["delay"] or 0) / 1000
            check("works on YouTube", y["overlay"] and y["aligned"] and ymed is not None and abs(ymed - target) < 0.035,
                  f"median lag {ymed and round(ymed * 1000)} ms vs target {round(target * 1000)} ms, aligned {y['aligned']}")
            page.screenshot(path=str(Path(root) / "youtube.png"))
        ctx.close()
    srv.shutdown()
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None) else 1)
