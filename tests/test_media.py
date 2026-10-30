"""Sonos play/pause <-> PC media. Launches your installed Chrome (temporary profile)
playing a local test video, then pauses/resumes it the way a Playbar button press would.
Needs a working audio output (monitor awake).

    python tests/test_media.py <dir with index.html + test.mp4>
"""
import functools
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sonos_cast as sc  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def wait(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def video_paused(port=9333):
    """Ask Chrome (DevTools protocol, via the page's title trick) whether the video is paused."""
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read())
    page = next(t for t in tabs if t["type"] == "page" and "8765" in t["url"])
    return page["title"]


def main(root):
    # the page reports its state in document.title so we can read it without a driver
    Path(root, "media.html").write_text(
        '<!doctype html><title>init</title><video id=v src="test.mp4" autoplay loop playsinline></video>'
        "<script>const v=document.getElementById('v');"
        "setInterval(()=>document.title=v.paused?'paused':'playing',200);</script>")
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
    h.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    prof = tempfile.mkdtemp()
    chrome = subprocess.Popen([CHROME, f"--user-data-dir={prof}", "--no-first-run", "--remote-debugging-port=9333",
                               "--autoplay-policy=no-user-gesture-required",
                               "http://127.0.0.1:8765/media.html"])
    try:
        ok = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                ok = video_paused() == "playing"
            except Exception:
                pass
            if ok:
                break
        check("Chrome is playing the test video", ok)
        time.sleep(2)
        audible, known = sc.MediaControl._audio_apps()
        check("Chrome shows up as audible to Windows", "chrome.exe" in audible, f"audible {sorted(audible)}")

        mc = sc.MediaControl()
        paused = mc.pause_audible()
        time.sleep(1)
        check("Sonos pause -> Chrome video paused", video_paused() == "paused", f"paused sessions {paused}")
        check("silent apps (e.g. Spotify playing on another device) left alone",
              not any("spotify" in p.lower() for p in paused), f"{paused}")
        mc.resume(paused)
        time.sleep(1)
        check("Sonos play -> Chrome video resumes", video_paused() == "playing")

        # the whole loop through the real speaker
        cfg = sc.load_config()
        cfg.update(auto_cast=False, keep_local=True)
        c = sc.SonosCaster(cfg, notify=lambda t, m: None)
        try:
            sp = c._coordinator()
            c.start()
            time.sleep(4)
            first_conn = c.broadcaster.newest(sp.ip_address)
            sp.pause()  # = Playbar touch button / pause in the Sonos app
            ok = wait(lambda: c.state == "paused" and video_paused() == "paused", 5)
            check("Playbar pause -> app 'paused' and the YouTube/Chrome video pauses", ok,
                  f"state {c.state}, page {video_paused()}, still casting {c.casting}")
            sp.play()
            ok = wait(lambda: c.state != "paused" and video_paused() == "playing", 6)
            check("Playbar play -> video resumes", ok, f"state {c.state}, page {video_paused()}")
            time.sleep(3)
            fresh = c.broadcaster.newest(sp.ip_address) is not first_conn
            lat = c.measure_latency(samples=3)
            check("after resume: fresh stream, delay still low (no stale pause buffer)",
                  fresh and lat is not None and lat < 0.25, f"fresh={fresh} buffer {lat and round(lat * 1000)} ms")
        finally:
            c.shutdown()
    finally:
        chrome.terminate()
        srv.shutdown()
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    saved = sc.CONFIG_FILE.read_text() if sc.CONFIG_FILE.exists() else None
    try:
        ok = main(sys.argv[1])
    finally:
        if saved is not None:
            sc.CONFIG_FILE.write_text(saved)  # the engine saves settings; put the user's back
    sys.exit(0 if ok else 1)
