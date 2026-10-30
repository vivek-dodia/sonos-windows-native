"""Pacing / drift-control test without any audio hardware.

Feeds LoopbackCapture a sine tone through its WASAPI callback at a sound-card
clock that runs fast or slow, then checks the paced output:
  * no gaps (noise blocks) once audio is flowing
  * the capture queue stays near its target (latency doesn't creep)
  * output rate tracks wall clock (+ rate_adj when the Sonos controller asks)

    python tests/test_pacing.py
"""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sonos_cast as sc  # noqa: E402


class FakeCapture(sc.LoopbackCapture):
    def _supervise(self):  # no real device
        pass


def run(drift_ppm, rate_adj=0, seconds=8.0):
    blocks = []
    cap = FakeCapture(blocks.append)
    cap.rate_adj = rate_adj
    rate = sc.OUT_RATE * (1 + drift_ppm / 1e6)
    n = 480
    stop = threading.Event()

    def feed():  # sound card delivering 10 ms callbacks on its own clock
        phase, t0, k = 0, time.perf_counter(), 0
        while not stop.is_set():
            idx = np.arange(phase, phase + n)
            s = (8000 * np.sin(2 * np.pi * 440 * idx / sc.OUT_RATE)).astype(np.int16)
            cap._on_audio(np.repeat(s, 2).tobytes(), n, None, 0)
            phase += n
            k += 1
            d = t0 + k * n / rate - time.perf_counter()
            if d > 0:
                time.sleep(d)

    time.sleep(0.05)
    threading.Thread(target=feed, daemon=True).start()
    t_start = time.perf_counter()
    first = len(blocks)
    time.sleep(seconds)
    last = len(blocks)
    stop.set()
    elapsed = time.perf_counter() - t_start
    with cap.buf_lock:
        level_ms = len(cap.buf) / 4 / sc.OUT_RATE * 1000
    cap.close()

    settled = blocks[int(len(blocks) * 0.25):]  # skip the priming phase
    peaks = [np.abs(np.frombuffer(b, np.int16)).max() for b in settled]
    gaps = sum(1 for p in peaks if p < 100)
    frames = sum(len(b) for b in blocks[first:last]) / 4
    out_rate = frames / elapsed
    expect = sc.OUT_RATE * (1 + rate_adj / 480)
    # distortion: energy outside the tone's band, relative to the tone (dB)
    x = np.frombuffer(b"".join(settled), np.int16)[::2].astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sc.OUT_RATE)
    tone = spec[(f > 400) & (f < 480)].sum()
    rest = spec[(f > 20) & ((f < 400) | (f > 480))].sum()
    snr = 10 * np.log10(tone / rest)
    ok = gaps == 0 and level_ms < 45 and abs(out_rate - expect) / expect < 0.002 and snr > 40
    print(f"{'PASS' if ok else 'FAIL'}  drift {drift_ppm:+6d} ppm  rate_adj {rate_adj:+d}: "
          f"gaps={gaps}  capture queue={level_ms:4.1f} ms  output {out_rate:8.0f} fr/s (expect ~{expect:.0f})"
          f"  clean={snr:5.1f} dB")
    return ok


if __name__ == "__main__":
    results = [run(0), run(+300), run(-300), run(+2000), run(-2000), run(0, rate_adj=+2), run(0, rate_adj=-2)]
    print("ALL PASS" if all(results) else "SOME FAILED")
    sys.exit(0 if all(results) else 1)
