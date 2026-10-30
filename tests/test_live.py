"""Live end-to-end tests against the real Sonos. Quit the tray app first (port 8080).

    python tests/test_live.py

Plays only inaudible noise / silence, but it does take over the speaker
(and briefly switches it to its TV input) while running.
"""
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sonos_cast as sc  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def wait_for(cond, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def safe_stop(sp):
    try:
        sp.stop()
    except Exception:  # e.g. on the TV input, which can't be "stopped"
        pass


def state(sp):
    return sp.get_current_transport_info()["current_transport_state"]


def http(path, host="127.0.0.1"):
    try:
        with urllib.request.urlopen(f"http://{host}:{sc.PORT}/{path}", timeout=3) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def main():
    notes = []
    saved_cfg = sc.CONFIG_FILE.read_text() if sc.CONFIG_FILE.exists() else None
    try:
        return run(notes)
    finally:
        if saved_cfg is not None:
            sc.CONFIG_FILE.write_text(saved_cfg)  # tests change options; put the user's back


def run(notes):
    cfg = sc.load_config()
    cfg.update(auto_cast=False, keep_local=True, format="wav", idle_minutes=0.1)
    c = sc.SonosCaster(cfg, notify=lambda t, m: notes.append(t))
    sp = c._coordinator()
    safe_stop(sp)
    time.sleep(1)

    # --- startup + latency -------------------------------------------------
    t = time.monotonic()
    c.start()
    ok = wait_for(lambda: state(sp) == "PLAYING", 5)
    check("WAV stream starts playing fast", ok and time.monotonic() - t < 1.5, f"{time.monotonic() - t:.2f}s")
    time.sleep(2)
    lat = sorted(c.measure_latency() for _ in range(5))
    check("Sonos buffer (delay) is small", lat[2] < 0.25, f"median {lat[2] * 1000:.0f} ms, samples {[round(x * 1000) for x in lat]}")
    check("single stream connection (no 10 s probe/reconnect loop)", len(c.broadcaster.clients) == 1,
          f"{len(c.broadcaster.clients)} connections")

    # --- closed-loop latency controller ------------------------------------
    c.LATENCY_TARGET, c.LATENCY_BAND = 0.30, (0.25, 0.35)
    ok = wait_for(lambda: c.latency is not None and abs(c.latency - 0.30) < 0.05, 120, 1)
    check("controller grows the buffer to a new target", ok, f"now {c.latency * 1000:.0f} ms")
    c.LATENCY_TARGET, c.LATENCY_BAND = 0.13, (0.09, 0.18)
    ok = wait_for(lambda: c.latency is not None and c.latency < 0.18 and c.capture.rate_adj == 0, 120, 1)
    check("controller drains it back without dropping the stream", ok and state(sp) == "PLAYING",
          f"now {c.latency * 1000:.0f} ms, state {state(sp)}")

    # --- local control API -------------------------------------------------
    code, body = http("status")
    check("status API", code == 200 and b'"latency_ms"' in body, body.decode()[:120])
    lan_ip = sc.local_ip_for(cfg["speaker_ip"])
    code, _ = http("stop", host=lan_ip)
    check("control API refuses non-localhost callers", code == 403 and c.casting, f"HTTP {code}")
    try:
        sc.SonosCaster(dict(cfg))
        check("second instance can't grab the port", False)
    except OSError:
        check("second instance can't grab the port", True)

    # --- Sonos-side events -------------------------------------------------
    sp.stop()  # looks like a dropped stream
    ok = wait_for(lambda: state(sp) == "PLAYING", 12, 0.5)
    check("reconnects after the Sonos drops the stream", ok and c.casting)

    sp.pause()
    ok = wait_for(lambda: c.state == "paused", 5, 0.2)
    check("pause on the Sonos -> app shows paused (keeps the speaker)", ok and c.casting, f"state {c.state}")
    sp.play()
    ok = wait_for(lambda: c.state != "paused" and state(sp) == "PLAYING", 6, 0.2)
    check("play on the Sonos -> resumes on a fresh stream", ok, f"state {c.state}")
    time.sleep(2)
    lat = c.measure_latency(samples=3)
    check("delay still low after pause/resume", lat is not None and lat < 0.25, f"{lat and round(lat * 1000)} ms")
    sp.switch_to_tv()
    ok = wait_for(lambda: not c.casting, 12, 0.5)
    check("switching the Sonos to TV releases it", ok, f"notes {notes[-1:]}")
    time.sleep(1)
    signal = sp.deviceProperties.GetZoneInfo()["HTAudioIn"]
    free = c._sonos_is_free()
    check("TV input with no TV audio counts as free; with TV audio as busy", free == (signal == "0"),
          f"HTAudioIn={signal}, free={free}")

    # --- auto-cast -----------------------------------------------------------
    c.start()  # leave the TV input: park the Sonos on our own (stopped) stream
    c.stop(user=False)
    c.set_option("auto_cast", True)
    playing = [False]

    def fake_sound():
        while True:
            if playing[0]:
                c.capture.last_sound = time.monotonic()
            time.sleep(0.2)
    threading.Thread(target=fake_sound, daemon=True).start()
    time.sleep(1.5)
    playing[0] = True
    check("auto-cast starts when the PC plays sound",
          wait_for(lambda: c.casting, 5) and c.auto_started)
    c.stop()
    time.sleep(3)
    check("manual stop is respected while sound continues", not c.casting)
    playing[0] = False
    time.sleep(11)
    playing[0] = True
    check("auto-cast resumes after a quiet gap", wait_for(lambda: c.casting, 5))
    playing[0] = False
    check("auto-stops after idle timeout", wait_for(lambda: not c.casting, 15, 0.5))
    c.set_option("auto_cast", False)

    # --- MP3 fallback --------------------------------------------------------
    c.cfg["format"] = "mp3"
    c.start()
    ok = wait_for(lambda: state(sp) == "PLAYING", 10)
    check("MP3 fallback format plays", ok)
    c.stop()
    c.cfg["format"] = "wav"

    c.shutdown()
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
