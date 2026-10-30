"""PC-side tests: speaker muting, volume link, mute key, real captured audio, and the tray menu.
Needs a working audio output (monitor awake). Quit the tray app first (port 8080).

    python tests/test_local.py
"""
import sys
import threading
import time
import urllib.request
import winsound
from pathlib import Path

import numpy as np
from pycaw.pycaw import AudioUtilities

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


def main():
    saved_cfg = sc.CONFIG_FILE.read_text() if sc.CONFIG_FILE.exists() else None
    ep = AudioUtilities.GetSpeakers().EndpointVolume
    before = (ep.GetMute(), round(ep.GetMasterVolumeLevelScalar() * 100))
    try:
        engine_tests(ep, before)
        tray_tests()
    finally:
        if saved_cfg is not None:
            sc.CONFIG_FILE.write_text(saved_cfg)
        ep.SetMasterVolumeLevelScalar(before[1] / 100, None)
        ep.SetMute(before[0], None)
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


def engine_tests(ep, before):
    cfg = sc.load_config()
    cfg.update(auto_cast=False, keep_local=False)
    c = sc.SonosCaster(cfg, notify=lambda t, m: None)
    check("loopback opened on the default output", wait_for(lambda: c.capture.name, 6), c.capture.name)
    sp = c._coordinator()
    sonos_before = (sp.volume, sp.mute)
    try:
        c.start()
        check("PC speakers muted while casting", wait_for(lambda: ep.GetMute() == 1, 3))
        check("Windows slider starts at the Sonos volume",
              wait_for(lambda: round(ep.GetMasterVolumeLevelScalar() * 100) == sp.volume, 3),
              f"slider {round(ep.GetMasterVolumeLevelScalar() * 100)} / sonos {sp.volume}")

        target = max(5, sonos_before[0] - 4)
        ep.SetMasterVolumeLevelScalar(target / 100, None)
        check("slider / volume keys -> Sonos volume", wait_for(lambda: sp.volume == target, 3), f"sonos {sp.volume}")

        sp.volume = sonos_before[0]
        check("Sonos app volume -> Windows slider",
              wait_for(lambda: round(ep.GetMasterVolumeLevelScalar() * 100) == sonos_before[0], 5),
              f"slider {round(ep.GetMasterVolumeLevelScalar() * 100)}")

        ep.SetMute(0, None)  # what the mute key does to an already-muted PC
        check("mute key mutes the Sonos (PC stays muted)",
              wait_for(lambda: sp.mute and ep.GetMute() == 1, 3), f"sonos mute {sp.mute}")
        ep.SetMute(0, None)
        check("mute key again unmutes the Sonos", wait_for(lambda: not sp.mute and ep.GetMute() == 1, 3))

        # real audio through the whole chain: Windows -> loopback -> HTTP stream
        grabbed = []

        def grab():
            with urllib.request.urlopen(f"http://127.0.0.1:{sc.PORT}/stream.wav", timeout=5) as r:
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    grabbed.append(r.read(19200))
        t = threading.Thread(target=grab)
        t.start()
        time.sleep(0.5)
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS)
        t.join()
        pcm = np.frombuffer(b"".join(grabbed)[44:], np.int16)
        peak = int(np.abs(pcm).max()) if pcm.size else 0
        check("a real Windows sound comes out of the stream (while the PC is muted)", peak > 1000, f"peak {peak}")
        check("the engine noticed the sound (auto-cast trigger)", c.sound_active(within=4))
        quiet = np.abs(pcm[: 48000 // 10 * 2]).max() if pcm.size else 99
        check("silence is sent as an inaudible noise floor", 0 < quiet <= 3, f"peak {quiet} (-80 dB)")
    finally:
        c.stop(user=False)
        check("PC mute + volume restored after casting",
              wait_for(lambda: (ep.GetMute(), round(ep.GetMasterVolumeLevelScalar() * 100)) == before, 3),
              f"{(ep.GetMute(), round(ep.GetMasterVolumeLevelScalar() * 100))} vs {before}")
        sp.volume, sp.mute = sonos_before
        c.shutdown()
        time.sleep(0.5)

    # PC shut down mid-cast: the app never got to unmute. Next launch must fix it.
    dev = AudioUtilities.GetSpeakers()
    ep.SetMute(1, None)
    sc.MUTED_FILE.write_text(sc.json.dumps({dev.id: [before[0], before[1] / 100]}))
    sc.restore_leftover_mutes()
    check("speakers left muted by a shutdown mid-cast are restored on next launch",
          ep.GetMute() == before[0] and not sc.MUTED_FILE.exists(), f"mute {ep.GetMute()}")


def walk(menu, depth=0):
    """Evaluate every dynamic text/checked/enabled callable, like Windows does when the menu opens."""
    n = 0
    for item in menu.items:
        _ = (item.text, item.checked, item.enabled, item.visible)
        n += 1
        if item.submenu:
            n += walk(item.submenu, depth + 1)
    return n


def tray_tests():
    import sonos_tray
    app = sonos_tray.TrayApp()
    try:
        n = walk(app.icon.menu)
        check("tray menu builds and every item renders", n > 15, f"{n} items")
        check("status line reads sensibly", app.status_line().startswith("Idle"), app.status_line())
        before = app.cfg["auto_cast"]
        app.caster.set_option("auto_cast", not before)
        check("auto-cast toggle flips and saves", sc.load_config()["auto_cast"] == (not before))
        app.caster.set_option("auto_cast", before)
        was = sonos_tray.autostart_enabled()
        sonos_tray.set_autostart(not was)
        flipped = sonos_tray.autostart_enabled() == (not was)
        sonos_tray.set_autostart(was)
        check("start-with-Windows toggle works", flipped and sonos_tray.autostart_enabled() == was)
        app._discover()
        check("speaker discovery fills the Speaker menu", wait_for(lambda: app.speakers, 8),
              ", ".join(s.player_name for s in app.speakers))
        walk(app.icon.menu)
        app.caster.start()
        check("status line while casting shows delay and volume",
              wait_for(lambda: "ms delay" in app.status_line(), 25, 0.5), app.status_line())
        check("tooltip fits Windows' 128-char limit", len(app.icon.title or "") < 128)
        app.caster.stop(user=False)
    finally:
        app.caster.shutdown()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
