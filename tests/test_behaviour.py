"""Behaviour tests with the PC side simulated (no audio device or headphones needed),
against the real Sonos. Quit the tray app first (port 8080).

    python tests/test_behaviour.py

Covers: notification dings don't trigger auto-cast, media playback does at once,
sustained sound (games) does after a few seconds, and switching the PC to
headphones hands the Sonos back and never casts private audio.
"""
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sonos_cast as sc  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def wait_for(cond, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class FakePC:
    """Drives the capture's sound detection like real audio would."""

    def __init__(self, cap):
        self.cap, self.on = cap, False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            if self.on:
                now = time.monotonic()
                if now - self.cap.last_sound > 0.7:
                    self.cap.sound_since = now
                self.cap.last_sound = now
            time.sleep(0.1)

    def ding(self, secs=0.6):
        self.on = True
        time.sleep(secs)
        self.on = False


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="    log: %(message)s")
    logging.getLogger("soco").setLevel(logging.WARNING)
    saved = sc.CONFIG_FILE.read_text() if sc.CONFIG_FILE.exists() else None
    real_default_output = sc.default_output
    headphones = [False]
    sc.default_output = lambda: ("fake", "Headphones (AirPods)" if headphones[0] else "HP X27q", headphones[0])
    notes = []
    cfg = sc.load_config()
    cfg.update(auto_cast=True, keep_local=True, idle_minutes=5)
    c = sc.SonosCaster(cfg, notify=lambda t, m: notes.append(t))
    media_playing = [False]
    c.media.playing_audible = lambda: media_playing[0]
    c.media.now_playing = lambda: "Big Buck Bunny - Blender" if media_playing[0] else None
    pc = FakePC(c.capture)
    try:
        sp = c._coordinator()
        c.start()
        c.stop(user=False)  # park the Sonos on our own stopped stream (free to take over)
        time.sleep(1)

        pc.ding()
        time.sleep(3)
        check("a notification ding does NOT wake the Sonos", not c.casting)

        time.sleep(11)  # quiet gap
        media_playing[0] = True
        pc.on = True
        check("a media player (YouTube...) starts auto-cast right away", wait_for(lambda: c.casting, 3))
        time.sleep(1.5)
        md = sp.avTransport.GetPositionInfo([("InstanceID", 0)])["TrackMetaData"]
        title = (re.findall(r"<dc:title>(.*?)</dc:title>", md) or [None])[0]
        check("the Sonos app shows what's playing", title == "Big Buck Bunny - Blender", f"title {title!r}")
        pc.on = False  # the video ends (stop the fake player before releasing, or auto-cast rightly restarts)
        media_playing[0] = False
        c.stop(user=False)
        time.sleep(11)

        pc.on = True  # e.g. a game: no media session, but continuous sound
        t = time.monotonic()
        ok = wait_for(lambda: c.casting, 8)
        took = time.monotonic() - t
        check("sustained sound (games) auto-casts after a few seconds", ok and 2.5 < took < 6, f"after {took:.1f}s")

        assert c.casting
        notes.clear()
        headphones[0] = True  # user connects AirPods: Windows makes them the default output
        ok = wait_for(lambda: not c.casting and notes, 6)
        check("switching to headphones stops casting (audio stays private) and says why",
              ok and notes[:1] == ["Stopped casting"], f"notes {notes}")
        check("capture is off while on headphones", wait_for(lambda: c.capture.stream is None, 3))
        time.sleep(3)
        check("no auto-cast while on headphones, even with sound", not c.casting)
        check("clicking cast on headphones explains instead of casting", c.start() is False and
              notes[-1] == "Headphones in use", f"notes {notes[-1:]}")

        headphones[0] = False
        time.sleep(11)  # hold_auto clears after quiet... sound is still on, so wait it out
        pc.on = False
        time.sleep(11)
        pc.on = True
        check("back on the monitor: auto-cast works again", wait_for(lambda: c.casting, 8))
    finally:
        pc.on = False
        c.shutdown()
        sc.default_output = real_default_output
        if saved is not None:
            sc.CONFIG_FILE.write_text(saved)
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
