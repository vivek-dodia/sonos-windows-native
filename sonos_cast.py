"""
Sonos Cast - send ALL Windows audio to a Sonos speaker.

Captures whatever your PC is playing (Chrome, VLC, games, anything) via
WASAPI loopback on the default output device and serves it as an endless
WAV (raw PCM) stream that the Sonos pulls over the LAN. WAV because the Sonos
pre-buffers a fixed number of bytes: at 1.5 Mbit/s that is ~0.12 s, vs ~4 s for MP3.

The tray app (sonos_tray.py) is the normal way to use this. The CLI is handy
for debugging:

    python sonos_cast.py                 # cast until Ctrl+C
    python sonos_cast.py --volume 30     # cast + set Sonos volume
    python sonos_cast.py --stop          # just stop the Sonos
"""
import argparse
import collections
import ctypes
import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.request
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import lameenc
import numpy as np
import pyaudiowpatch as pyaudio
import soco

log = logging.getLogger("sonos-cast")

PORT = 8080  # the Sonos pulls the stream from here: allow it through the Windows firewall

OUT_RATE = 48000
FRAME_MS = 10                  # pacing tick
MAX_BACKLOG_MS = 80            # drop captured audio older than this (clock drift guard)
SOUND_THRESHOLD = 40           # int16 peak that counts as "the PC is playing something"

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "SonosCast"
CONFIG_FILE = APP_DIR / "config.json"
DEFAULTS = {
    "speaker_ip": None,        # found on the network on first run (then remembered)
    "speaker_name": None,
    "auto_cast": True,         # start casting when the PC makes sound
    "idle_minutes": 5,         # auto-stop after this much silence (auto-started casts only)
    "keep_local": False,       # True = don't mute PC speakers / link the volume slider
    "format": "wav",           # wav: ~0.12 s delay. mp3: ~3.7 s (the Sonos buffers by bytes), 5x less bandwidth
    "bitrate": 320,            # mp3 only
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def local_ip_for(target):
    """IP of the interface that routes to the Sonos (avoids ZeroTier/Tailscale/etc)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1400))
        return s.getsockname()[0]
    finally:
        s.close()


FORM_FACTOR_KEY = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e} 0"  # PKEY_AudioEndpoint_FormFactor
PRIVATE_FORM_FACTORS = {3, 5}  # Headphones, Headset (AirPods, WH-1000XM5, 3.5 mm jack...)


def default_output():
    """(id, name, is_headphones) for Windows' default playback device, or None if there is none."""
    import warnings
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    try:
        with warnings.catch_warnings():  # pycaw warns about every property it can't read
            warnings.simplefilter("ignore")
            dev = AudioUtilities.GetSpeakers()
            props = dev.properties or {}
        ff = next((v for k, v in props.items() if k.lower() == FORM_FACTOR_KEY), None)
        return dev.id, dev.FriendlyName, ff in PRIVATE_FORM_FACTORS
    except Exception:
        return None


def find_speaker(ip, name=None):
    """The configured IP if it answers, else find the speaker by room name (DHCP moved it).
    With nothing configured yet (first run), the first speaker found, by room name."""
    if ip:
        try:
            sp = soco.SoCo(ip)
            sp.player_name  # noqa: B018 - raises if unreachable
            return sp
        except Exception:
            pass
    for sp in sorted(soco.discover(timeout=3) or (), key=lambda s: s.player_name):
        if name is None or sp.player_name == name:
            return sp
    return None


class Client:
    def __init__(self, addr):
        self.addr = addr
        self.queue = collections.deque(maxlen=512)
        self.event = threading.Event()
        self.frames_sent = 0  # audio handed to the socket - minus Sonos play position = its buffer
        self.opened = time.monotonic()


class Broadcaster:
    """Fan-out of PCM blocks to every connected HTTP client."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.on_close = lambda client: None
        self.stall_until = 0.0  # tests only

    def add(self, addr):
        client = Client(addr)
        with self.lock:
            self.clients.add(client)
        return client

    def newest(self, addr):
        """The speaker's current stream connection."""
        with self.lock:
            mine = [c for c in self.clients if c.addr == addr]
        return max(mine, key=lambda c: c.opened) if mine else None

    def remove(self, client):
        with self.lock:
            self.clients.discard(client)

    def publish(self, data):
        if not self.clients:
            return
        with self.lock:
            for client in self.clients:
                client.queue.append(data)
                client.event.set()


class LoopbackCapture:
    """WASAPI loopback -> paced, gap-free PCM.

    Windows delivers nothing while the PC is silent, so a pacing thread emits
    fixed-size frames on a wall clock and pads the gaps with an inaudible noise
    floor (about -80 dB). Pure digital zeros make the Sonos spend 10 s probing
    the stream before it plays.

    Two tiny rate controls keep the latency pinned (each one stretches or
    squeezes a 10 ms block by a few samples, about 0.4%, which you can't hear):
      * capture side: keep ~15 ms of captured audio queued (sound card clock vs ours)
      * output side: `rate_adj`, set by the Sonos buffer controller (Sonos clock vs ours)
    """

    rate = OUT_RATE
    channels = 2
    bytes_per_frame = 4  # s16le stereo
    CAPTURE_TARGET_MS = 15

    def __init__(self, sink, device_index=None):
        self.sink = sink
        self.device_index = device_index
        self.name = None
        self.pa = None
        self.stream = None
        self.dev_rate = OUT_RATE
        self.dev_channels = 2
        self.last_sound = 0.0  # monotonic time the PC last made audible sound
        self.sound_since = 0.0  # start of the current stretch of sound (gaps < 0.7 s don't count)
        self.headphones = False  # default output is headphones: not capturing
        self.rate_adj = 0      # frames added (+) / removed (-) per 10 ms block sent
        self.input_latency = 0.01  # WASAPI's, once a device is open
        self.queue_avg = float(int(OUT_RATE * self.CAPTURE_TARGET_MS / 1000))  # frames
        self.noise = np.random.default_rng().integers(-2, 3, OUT_RATE * 2, dtype=np.int16)
        self.buf = bytearray()
        self.buf_lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._pace, daemon=True).start()
        threading.Thread(target=self._supervise, daemon=True).start()

    @staticmethod
    def _pick_device(pa, device_index):
        if device_index is not None:
            return pa.get_device_info_by_index(device_index)
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if out.get("isLoopbackDevice"):
            return out
        for lb in pa.get_loopback_device_info_generator():
            if out["name"] in lb["name"]:
                return lb
        raise RuntimeError(f"No loopback device for default output '{out['name']}'")

    def _supervise(self):
        """(Re)open the loopback when the default output changes or disappears
        (HDMI/monitor audio drops out when the display sleeps, Bluetooth, etc)."""
        warned = False
        while self.running:
            out = default_output()
            self.headphones = bool(out and out[2])
            if self.headphones:
                # Private listening: never capture (or cast) what goes to headphones.
                if self.stream is not None:
                    log.info("default output is headphones (%s) - not capturing", out[1])
                    self._close_stream()
                time.sleep(2)
                continue
            want = None
            probe = pyaudio.PyAudio()  # fresh instance = fresh device list
            try:
                want = self._pick_device(probe, self.device_index)["name"]
            except Exception:
                pass
            finally:
                probe.terminate()

            alive = self.stream is not None and self.stream.is_active()
            if want and (want != self.name or not alive):
                self._close_stream()
                try:
                    self._open(want)
                    log.info("capturing : %s (%d Hz)", self.name, self.dev_rate)
                    warned = False
                except Exception as e:
                    self._close_stream()
                    if not warned:
                        log.warning("can't open %s (%s) - retrying", want, e)
                        warned = True
            elif not want and not warned:
                log.warning("no audio output device (monitor asleep?) - sending silence until it's back")
                self._close_stream()
                warned = True
            time.sleep(2)

    def _open(self, name):
        self.pa = pyaudio.PyAudio()
        dev = self._pick_device(self.pa, self.device_index)
        self.dev_rate = int(dev["defaultSampleRate"])
        self.dev_channels = max(1, dev["maxInputChannels"])
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.dev_channels,
            rate=self.dev_rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(self.dev_rate * FRAME_MS / 1000),
            stream_callback=self._on_audio,
        )
        self.input_latency = self.stream.get_input_latency()
        self.name = name

    def pipeline_s(self):
        """Capture-side delay: WASAPI's input latency + how much audio sits in our queue."""
        return self.input_latency + self.queue_avg / self.rate

    def _close_stream(self):
        self.name = None
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None

    def _on_audio(self, data, frame_count, time_info, status):
        if self.dev_channels != 2 or self.dev_rate != OUT_RATE:
            data = self._convert(data)
        samples = np.frombuffer(data, dtype=np.int16)
        if samples.size and max(samples.max(), -samples.min()) > SOUND_THRESHOLD:
            now = time.monotonic()
            if now - self.last_sound > 0.7:
                self.sound_since = now
            self.last_sound = now
        with self.buf_lock:
            self.buf += data
            limit = int(self.rate * MAX_BACKLOG_MS / 1000) * self.bytes_per_frame
            if len(self.buf) > limit:
                del self.buf[: len(self.buf) - limit]
        return (None, pyaudio.paContinue)

    def _convert(self, data):
        """Any channel count / rate -> 48 kHz stereo s16le."""
        a = np.frombuffer(data, dtype=np.int16).reshape(-1, self.dev_channels).astype(np.float32)
        if self.dev_channels == 1:
            a = np.repeat(a, 2, axis=1)
        elif self.dev_channels > 2:
            a = a[:, :2]  # front L/R
        if self.dev_rate != OUT_RATE:
            n = int(round(len(a) * OUT_RATE / self.dev_rate))
            src = np.linspace(0, len(a) - 1, n)
            a = np.stack([np.interp(src, np.arange(len(a)), a[:, c]) for c in (0, 1)], axis=1)
        return np.clip(a, -32768, 32767).astype(np.int16).tobytes()

    def _noise(self, frames):
        start = np.random.randint(0, len(self.noise) // 2 - frames * 2)
        return self.noise[start:start + frames * 2].tobytes()

    @staticmethod
    def _stretch(pcm, frames):
        """Resample a block of s16le stereo to exactly `frames` frames."""
        a = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2).astype(np.float32)
        if len(a) == frames:
            return pcm
        src = np.linspace(0, len(a) - 1, frames)
        idx = np.arange(len(a))
        out = np.stack([np.interp(src, idx, a[:, 0]), np.interp(src, idx, a[:, 1])], axis=1)
        return out.astype(np.int16).tobytes()

    def _pace(self):
        n = int(self.rate * FRAME_MS / 1000)
        target = int(self.rate * self.CAPTURE_TARGET_MS / 1000)
        bpf = self.bytes_per_frame
        next_t = time.perf_counter()
        priming = True  # after a gap, let `target` frames queue up so playback doesn't stutter
        while self.running:
            emit = n + self.rate_adj
            with self.buf_lock:
                level = len(self.buf) // bpf
                if level == 0:
                    priming = True
                elif level >= target:
                    priming = False
                take = n
                if priming:
                    take = 0
                elif level > 2 * target:
                    take = n + 2       # capture running ahead: consume a bit faster
                elif level < target // 2:
                    take = n - 2       # running dry: consume a bit slower
                take = min(take, level)
                if not priming:
                    self.queue_avg += (level - self.queue_avg) * 0.01
                pcm = bytes(self.buf[:take * bpf])
                del self.buf[:take * bpf]
            if take >= n - 2:
                out = self._stretch(pcm, emit)
            elif take == 0:
                out = self._noise(emit)
            else:  # device went quiet / disappeared mid-block
                out = pcm + self._noise(emit - take)
            if not out.strip(b"\x00"):
                out = self._noise(emit)  # app is sending digital silence
            self.sink(out)
            next_t += FRAME_MS / 1000
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:  # fell way behind (sleep/hibernate) - resync
                next_t = time.perf_counter()

    def close(self):
        self.running = False
        self._close_stream()


def wav_header(rate=OUT_RATE, channels=2):
    """RIFF header for an endless 16-bit PCM stream (sizes maxed out)."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def mp3_encoder(bitrate):
    """Fallback format (much smaller, but the Sonos then buffers ~3.7 s instead of ~0.12 s).
    Encoded in-process: ffmpeg 8 on Windows fails with EINVAL streaming MP3 to a stdout pipe."""
    lame = lameenc.Encoder()
    lame.set_bit_rate(int(bitrate))
    lame.set_in_sample_rate(OUT_RATE)
    lame.set_channels(2)
    lame.set_quality(2)
    return lame


def make_handler(broadcaster, commands, bitrate=320):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_HEAD(self):
            self._headers()

        def _headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav" if self.path.startswith("/stream.wav") else "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("icy-name", "PC Audio")
            self.end_headers()

        def do_GET(self):
            cmd = self.path.strip("/")
            if cmd in commands:
                # local control (second launch / scripts); never from the LAN
                if self.client_address[0] != "127.0.0.1":
                    self.send_error(403)
                    return
                body = json.dumps(commands[cmd]()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            wav = self.path.startswith("/stream.wav")
            if not wav and not self.path.startswith("/stream.mp3"):
                self.send_error(404)
                return
            self._headers()
            lame = None if wav else mp3_encoder(bitrate)
            if wav:
                self.wfile.write(wav_header())
            client = broadcaster.add(self.client_address[0])
            q, ev = client.queue, client.event
            log.debug("%s connected", self.client_address[0])
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                while True:
                    ev.wait(1.0)
                    ev.clear()
                    while time.monotonic() < broadcaster.stall_until:  # tests: simulate a Wi-Fi stall
                        time.sleep(0.01)
                    while q:
                        pcm = q.popleft()
                        self.wfile.write(pcm if wav else bytes(lame.encode(pcm)))
                        client.frames_sent += len(pcm) // 4
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                broadcaster.remove(client)
                broadcaster.on_close(client)
                log.debug("%s disconnected", self.client_address[0])

        def log_message(self, *args):
            pass

    return Handler


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Fails to bind if another copy already owns the port (Windows' SO_REUSEADDR
    would silently let two copies share it - that's how stale servers hijack the stream)."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def send_command(cmd, port=PORT):
    """Talk to an already-running instance. Returns its reply, or None if none is running."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/{cmd}", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


MUTED_FILE = APP_DIR / "muted-devices.json"  # survives a shutdown mid-cast


def restore_leftover_mutes():
    """If the PC was shut down / the app killed while casting, the speakers are still
    muted (Windows keeps mute across reboots). Put them back the way they were."""
    try:
        saved = json.loads(MUTED_FILE.read_text())
    except (OSError, ValueError):
        return
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    for dev_id, (mute, level) in saved.items():
        try:
            dev = AudioUtilities.CreateDevice(AudioUtilities.GetDeviceEnumerator().GetDevice(dev_id))
            dev.EndpointVolume.SetMasterVolumeLevelScalar(level, None)
            dev.EndpointVolume.SetMute(mute, None)
            log.info("restored %s after an unclean exit", dev.FriendlyName)
        except Exception:
            pass  # device not present right now; it was unmuted by hand or will be next time
    try:
        MUTED_FILE.unlink()
    except OSError:
        pass


class LocalVolume:
    """Mutes the PC speakers and links the Windows volume slider / keyboard keys
    with the Sonos volume (both ways - changes in the Sonos app move the slider).

    WASAPI loopback is captured before endpoint volume and mute, so the Sonos
    keeps getting full-level audio while the PC itself stays silent.
    """

    def __init__(self, speaker, on_volume=None, on_mute=None):
        self.speaker = speaker
        self.on_volume = on_volume or (lambda v: None)
        self.on_mute = on_mute or (lambda m: None)
        self.volume = None
        self.running = True
        self.saved = {}  # device id -> (endpoint, mute, level) to restore on exit
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        import comtypes
        from pycaw.pycaw import AudioUtilities
        comtypes.CoInitialize()
        ep, dev_id, last, refresh, poll_sonos = None, None, None, 0.0, 0.0
        while self.running:
            try:
                now = time.monotonic()
                if ep is None or now > refresh:  # default output may change
                    out = default_output()
                    refresh = now + 1
                    if out is None or out[2]:
                        ep = dev_id = None  # no device, or headphones: never mute/move those
                        time.sleep(0.5)
                        continue
                    if out[0] != dev_id:
                        dev = AudioUtilities.GetSpeakers()
                        ep, dev_id = dev.EndpointVolume, dev.id
                        if dev.id not in self.saved:
                            self.saved[dev.id] = (ep, ep.GetMute(), ep.GetMasterVolumeLevelScalar())
                            self._persist()
                        last = None
                if not ep.GetMute():
                    # PC is kept muted, so an "unmute" here means the mute key was pressed:
                    # mute/unmute the Sonos instead.
                    if last is not None:
                        self.speaker.mute = not self.speaker.mute
                        self.on_mute(self.speaker.mute)
                    ep.SetMute(1, None)
                if last is None or now > poll_sonos:
                    # Sonos -> slider (initial sync, and changes made in the Sonos app)
                    sv = self.speaker.volume
                    if sv != last:
                        ep.SetMasterVolumeLevelScalar(sv / 100, None)
                        last = sv
                        self._report(sv)
                    poll_sonos = now + 2
                level = round(ep.GetMasterVolumeLevelScalar() * 100)
                if level != last:  # slider -> Sonos
                    self.speaker.volume = level
                    last = level
                    self._report(level)
            except Exception:
                ep = dev_id = None
                last = None
            time.sleep(0.2)
        self._restore()

    def _report(self, v):
        if v != self.volume:
            self.volume = v
            self.on_volume(v)

    def _persist(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            MUTED_FILE.write_text(json.dumps({k: [m, lv] for k, (_, m, lv) in self.saved.items()}))
        except OSError:
            pass

    def _restore(self):
        for ep, mute, level in self.saved.values():
            try:
                ep.SetMasterVolumeLevelScalar(level, None)
                ep.SetMute(mute, None)
            except Exception:
                pass
        try:
            MUTED_FILE.unlink()
        except OSError:
            pass

    def restore(self):
        self.running = False
        self.thread.join(timeout=2)


class MediaControl:
    """Pause/resume the PC's media players (YouTube in Chrome, Spotify, VLC...)
    through Windows' media sessions, so the Sonos' own play/pause (the Playbar's
    touch button, the Sonos app) controls what's playing on the PC.

    Only sessions whose app is actually making sound on this PC are paused:
    Windows also lists apps that are just remote-controlling playback elsewhere
    (Spotify Connect, for one) as "playing"."""

    PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING

    @staticmethod
    def _audio_apps():
        """(audible, known): lower-case exe names with an active audio session on the
        default output, and with any audio session at all."""
        import comtypes
        from pycaw.pycaw import AudioUtilities
        comtypes.CoInitialize()
        try:
            sessions = [s for s in AudioUtilities.GetAllSessions() if s.Process]
        except Exception:
            return set(), set()  # no output device: nothing can be playing
        known = {s.Process.name().lower() for s in sessions}
        audible = {s.Process.name().lower() for s in sessions if s.State == 1}  # AudioSessionStateActive
        return audible, known

    @staticmethod
    def _app_matches(app_id, exes):
        # media-session ids look like "Chrome", "MSEdge", "Spotify.exe", "VideoLAN.VLC",
        # or an opaque hash (Firefox uses one of its install path)
        app = app_id.lower()
        for exe in exes:
            stem = exe.removesuffix(".exe")
            if app == exe or app.startswith(stem) or (len(stem) >= 3 and stem in app):
                return True
        return False

    def _pick_sessions(self, playing_ids, audible, known):
        """Which playing media sessions to pause.

        * id names an app that's making sound here ("chrome.exe", "Chrome") -> yes
        * id names an exe that is NOT making sound here -> never. Spotify reports
          "Spotify.exe ... PLAYING" while it plays on your phone via Spotify Connect.
        * opaque id (Firefox uses a hash of its install path) -> only if some
          audible app here is still unaccounted for by the sessions above."""
        picked = [a for a in playing_ids if self._app_matches(a, audible)]
        unaccounted = {exe for exe in audible if not any(self._app_matches(a, {exe}) for a in picked)}
        if unaccounted:
            picked += [a for a in playing_ids
                       if a not in picked and not a.lower().endswith(".exe") and not self._app_matches(a, known)]
        return picked

    def _run(self, coro_fn):
        import asyncio
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

        async def go():
            return await coro_fn(await Manager.request_async())
        result = []

        def worker():  # own thread: asyncio.run can't nest inside a caller's event loop
            try:
                result.extend(asyncio.run(go()))
            except Exception:
                log.exception("media control failed")
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=5)
        return result

    def pause_audible(self):
        """Pause every media session that's playing *and* audible here. Returns their ids."""
        audible, known = self._audio_apps()

        async def pause(mgr):
            sessions = [s for s in mgr.get_sessions() if s.get_playback_info().playback_status == self.PLAYING]
            targets = self._pick_sessions([s.source_app_user_model_id for s in sessions], audible, known)
            paused = []
            for s in sessions:
                if s.source_app_user_model_id in targets and await s.try_pause_async():
                    paused.append(s.source_app_user_model_id)
            return paused
        return self._run(pause)

    def playing_audible(self):
        """Is a media player playing that's making sound on this PC?"""
        audible, known = self._audio_apps()
        if not audible:
            return False

        async def check(mgr):
            ids = [s.source_app_user_model_id for s in mgr.get_sessions()
                   if s.get_playback_info().playback_status == self.PLAYING]
            return self._pick_sessions(ids, audible, known)
        return bool(self._run(check))

    def now_playing(self):
        """'Title - Artist' of the media playing audibly here, or None."""
        audible, known = self._audio_apps()

        async def get(mgr):
            sessions = [s for s in mgr.get_sessions() if s.get_playback_info().playback_status == self.PLAYING]
            picked = self._pick_sessions([s.source_app_user_model_id for s in sessions], audible, known)
            for s in sessions:
                if s.source_app_user_model_id in picked:
                    p = await s.try_get_media_properties_async()
                    if p and p.title:
                        return [" - ".join(x for x in (p.title, p.artist) if x)]
            return []
        out = self._run(get)
        return out[0] if out else None

    def resume(self, app_ids):
        async def play(mgr):
            resumed = []
            for s in mgr.get_sessions():
                if s.source_app_user_model_id in app_ids and await s.try_play_async():
                    resumed.append(s.source_app_user_model_id)
            return resumed
        return self._run(play) if app_ids else []


class SonosCaster:
    """The whole engine: always capturing (so it can notice when the PC starts
    making sound), and casting to the Sonos when asked to or when auto-cast
    kicks in.

    States: "off" (not casting), "casting", "no-device" (casting, but the PC
    has no audio output right now), "unreachable" (can't find the Sonos).
    """

    def __init__(self, cfg, port=PORT, device=None, on_change=None, notify=None):
        self.cfg = cfg
        self.port = port
        self.on_change = on_change or (lambda: None)
        self.notify = notify or (lambda title, msg: log.info("%s: %s", title, msg))
        self.lock = threading.RLock()

        self.casting = False
        self.auto_started = False
        self.hold_auto = False      # user stopped manually: no auto-cast until the PC goes quiet
        self.next_auto_try = 0.0
        self.unreachable = False
        self.local = None
        self.volume = None
        self.muted = False
        self.speaker = None
        self.media = MediaControl()
        self.sonos_paused = False   # paused from the Sonos (Playbar button / app); we paused the PC too
        self.paused_apps = []
        self.paused_at = 0.0
        self.wake = threading.Event()  # set when the Sonos' stream connection closes: check right away

        self.broadcaster = Broadcaster()
        self.broadcaster.on_close = lambda client: self.casting and self.wake.set()
        self.server = ExclusiveHTTPServer(("0.0.0.0", port), make_handler(self.broadcaster, {
            "toggle": lambda: {"casting": self.toggle()},
            "start": lambda: {"casting": self.start()},
            "stop": lambda: {"casting": self.stop() or False},
            "status": lambda: {"state": self.state, "speaker": self.speaker_name, "volume": self.volume,
                               "latency_ms": None if self.latency is None else round(self.latency * 1000),
                               "av_delay_ms": self.av_delay_ms(),
                               "rate_adj": self.capture.rate_adj, "format": self.cfg.get("format", "wav")},
        }, cfg["bitrate"]))
        restore_leftover_mutes()  # only once we own the port: a 2nd launch must not undo our mute
        self.capture = LoopbackCapture(self.broadcaster.publish, device)
        self.latency = None  # seconds of audio queued in the Sonos (measured)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._latency_control, daemon=True).start()

    # ---- status -------------------------------------------------------

    @property
    def speaker_name(self):
        return self.cfg.get("speaker_name") or "Sonos"

    @property
    def state(self):
        if self.unreachable:
            return "unreachable"
        if not self.casting:
            return "off"
        if self.sonos_paused:
            return "paused"
        return "casting" if self.capture.name else "no-device"

    def av_delay_ms(self):
        """How far the Sonos sound trails the picture right now (None when not casting).
        The Chrome extension and play-synced.bat hold video back by this much.
        = audio queued in the Sonos (measured) + capture side (measured: WASAPI's
        reported input latency + the average of our own capture queue)."""
        if not self.casting or self.latency is None:
            return None
        return round((self.latency + self.capture.pipeline_s()) * 1000)

    def sound_active(self, within=1.0):
        return time.monotonic() - self.capture.last_sound < within

    # ---- control ------------------------------------------------------

    def _coordinator(self):
        if self.speaker is None:
            self.speaker = find_speaker(self.cfg["speaker_ip"], self.cfg.get("speaker_name"))
            if self.speaker is None:
                raise ConnectionError(f"can't find {self.speaker_name} on the network")
            if (self.speaker.ip_address, self.speaker.player_name) != (self.cfg["speaker_ip"], self.cfg["speaker_name"]):
                self.cfg["speaker_ip"], self.cfg["speaker_name"] = self.speaker.ip_address, self.speaker.player_name
                save_config(self.cfg)
                self.on_change()
        return self.speaker.group.coordinator

    @property
    def wav(self):
        return self.cfg.get("format", "wav") == "wav"

    @property
    def stream_url(self):
        ext = "wav" if self.wav else "mp3"
        return f"http://{local_ip_for(self.cfg['speaker_ip'])}:{self.port}/stream.{ext}"

    def is_ours(self, uri):
        """Is the Sonos playing our stream? (The WAV URL ends in the track title, so compare the start.)"""
        if self.wav:
            return uri.startswith(self.stream_url)
        return uri == self.stream_url.replace("http://", "x-rincon-mp3radio://", 1)

    def _play(self, sp):
        """WAV: tell the Sonos up front it's raw 16-bit PCM. That plus the noise floor
        gets it playing in ~0.2 s with only ~0.12 s buffered (MP3 radio: ~3.7 s)."""
        # what the Sonos app shows: the YouTube video / song title when we can tell
        title = self.media.now_playing() or "PC Audio"
        if not self.wav:
            sp.play_uri(self.stream_url, title=title, force_radio=True)
            return
        # The Sonos app shows the last part of the URL as the track title.
        url = self.stream_url + ("" if title == "PC Audio" else "/" + quote(title[:100], safe=""))
        meta = ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                f'<item id="1" parentID="0" restricted="1"><dc:title>{escape(title)}</dc:title>'
                f'<dc:creator>{escape(self.speaker_name if title == "PC Audio" else "PC Audio")}</dc:creator>'
                '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
                f'<res protocolInfo="http-get:*:audio/L16;rate={OUT_RATE};channels=2:*">{escape(url)}</res>'
                '</item></DIDL-Lite>')
        sp.avTransport.SetAVTransportURI([("InstanceID", 0), ("CurrentURI", url), ("CurrentURIMetaData", meta)])
        sp.play()

    def start(self, auto=False):
        with self.lock:
            if self.casting:
                return True
            if self.capture.headphones:
                if not auto:
                    self.notify("Headphones in use", "The PC's output is headphones. Switch it back to the "
                                                     "monitor/speakers to cast to the Sonos.")
                return False
            try:
                sp = self._coordinator()
                self._play(sp)
                self.volume, self.muted = sp.volume, sp.mute
            except Exception as e:
                self.speaker = None
                if not self.unreachable:
                    self.notify("Can't reach Sonos", f"{self.speaker_name}: {e}")
                self.unreachable = True
                self.on_change()
                return False
            self.unreachable = False
            self.casting, self.auto_started, self.hold_auto = True, auto, False
            self.sonos_paused, self.paused_apps = False, []
            if not self.cfg["keep_local"]:
                self.local = LocalVolume(self.speaker, self._on_volume, self._on_mute)
            log.info("casting to %s (%s)", self.speaker_name, "auto" if auto else "manual")
            if not self.cfg.get("tip_shown"):  # the icon says the rest; only explain it once
                self.cfg["tip_shown"] = True
                save_config(self.cfg)
                self.notify(f"Casting to {self.speaker_name}",
                            "PC audio now plays on the Sonos. Volume keys and the mute key control it, "
                            "and the Sonos' play/pause controls the PC.")
        self.on_change()
        return True

    def stop(self, reason=None, user=True, stop_sonos=True, quiet=False):
        with self.lock:
            if not self.casting:
                return
            self.casting = False
            self.sonos_paused = False
            if user and self.cfg["auto_cast"]:
                self.hold_auto = True
            if self.local:
                self.local.restore()
                self.local = None
            if stop_sonos:
                try:
                    self._coordinator().stop()
                except Exception:
                    pass
            log.info("stopped casting%s", f" ({reason})" if reason else "")
            if reason and not quiet:
                self.notify("Stopped casting", reason)
        self.on_change()

    def toggle(self):
        if self.casting:
            self.stop()
            return False
        return self.start()

    def set_volume(self, v):
        try:
            self._coordinator().volume = v
            self._on_volume(v)
        except Exception:
            pass

    def _on_volume(self, v):
        self.volume = v
        self.on_change()

    def _on_mute(self, muted):
        self.muted = muted
        self.on_change()

    def toggle_mute(self):
        try:
            sp = self._coordinator()
            sp.mute = not sp.mute
            self._on_mute(sp.mute)
        except Exception:
            pass

    def set_option(self, key, value):
        with self.lock:
            self.cfg[key] = value
            save_config(self.cfg)
            if key == "keep_local" and self.casting:
                if value and self.local:
                    self.local.restore()
                    self.local = None
                elif not value and not self.local:
                    self.local = LocalVolume(self.speaker, self._on_volume, self._on_mute)
            if key == "auto_cast":
                self.hold_auto = False
        self.on_change()

    def choose_speaker(self, sp):
        was = self.casting
        self.stop(user=False)
        self.cfg["speaker_ip"], self.cfg["speaker_name"] = sp.ip_address, sp.player_name
        save_config(self.cfg)
        self.speaker = None
        if was:
            self.start()
        self.on_change()

    def shutdown(self):
        self.stop(user=False)
        self.capture.close()
        self.server.shutdown()
        self.server.server_close()  # release the port

    # ---- background ---------------------------------------------------

    def _watch(self):
        while True:
            woke = self.wake.wait(1)
            self.wake.clear()
            if woke:
                time.sleep(0.3)  # let the Sonos settle into its new state
            try:
                self._tick(check_sonos=True)  # 2 quick SOAP calls/s while casting, so buttons feel instant
            except Exception:
                log.exception("watch tick failed")

    def _tick(self, check_sonos):
        quiet_for = time.monotonic() - self.capture.last_sound
        if self.hold_auto and quiet_for > 10:
            self.hold_auto = False

        if self.capture.headphones:
            if self.casting:
                self.stop("Headphones are now the PC's output - your audio stays in them.", user=False)
            return

        if not self.casting:
            if (self.cfg["auto_cast"] and not self.hold_auto and self.sound_active()
                    and time.monotonic() >= self.next_auto_try and self._real_playback()):
                if self._sonos_is_free():
                    self.start(auto=True)
                if not self.casting:  # busy (TV, Spotify...) or unreachable: ask again later
                    self.next_auto_try = time.monotonic() + 15
            return

        idle = self.cfg["idle_minutes"] * 60
        if self.auto_started and not self.sonos_paused and quiet_for > idle:
            self.stop(f"PC has been quiet for {self.cfg['idle_minutes']} min", user=False, quiet=True)
            return
        if self.sonos_paused and time.monotonic() - self.paused_at > idle:
            self.stop(f"paused for {self.cfg['idle_minutes']} min", user=False, quiet=True)
            return

        if check_sonos:
            sp = self._coordinator()
            uri = sp.avTransport.GetMediaInfo([("InstanceID", 0)])["CurrentURI"]
            state = sp.get_current_transport_info()["current_transport_state"]
            if not self.is_ours(uri):
                self.stop(f"{self.speaker_name} switched to something else", user=True, stop_sonos=False)
            elif state == "PAUSED_PLAYBACK" and not self.sonos_paused:
                self._sonos_paused()
            elif state == "PLAYING" and self.sonos_paused:
                self._sonos_resumed(sp)
            elif self.sonos_paused and time.monotonic() - self.paused_at > 2 and self.sound_active():
                log.info("PC started playing while the Sonos was paused - resuming the Sonos")
                self.sonos_paused = False
                self._play(sp)
            elif state == "STOPPED":
                log.info("Sonos dropped the stream - reconnecting")
                self._play(sp)
            self.on_change()

    def _sonos_paused(self):
        """Pause on the Playbar / in the Sonos app: pause what's playing on the PC too."""
        self.sonos_paused, self.paused_at = True, time.monotonic()
        self.paused_apps = self.media.pause_audible()
        log.info("paused from the Sonos; paused on the PC: %s", self.paused_apps or "nothing")

    def _sonos_resumed(self, sp):
        """Play on the Sonos after a pause. The Sonos would resume from the stale audio it
        buffered while paused, so start a fresh stream (~0.2 s) to keep the delay at ~0.15 s."""
        self.sonos_paused = False
        self._play(sp)
        resumed = self.media.resume(self.paused_apps)
        log.info("resumed from the Sonos; resumed on the PC: %s", resumed or "nothing")
        self.paused_apps = []

    # Sonos buffer controller. Below ~0.08 s the Sonos underruns and drops the stream;
    # left alone, clock drift between PC and Sonos (tens of ppm) slowly grows or drains it.
    LATENCY_TARGET = 0.13
    LATENCY_BAND = (0.09, 0.18)  # start correcting outside this
    LATENCY_SETTLE = 0.02        # stop correcting within this of the target

    def _reltime(self, sp):
        h, m, s = (int(x) for x in sp.avTransport.GetPositionInfo([("InstanceID", 0)])["RelTime"].split(":"))
        return h * 3600 + m * 60 + s

    def measure_latency(self, samples=1):
        """Seconds of audio sitting in the Sonos: audio sent minus play position.

        RelTime only has 1 s resolution, so catch the instant it ticks over - right
        then the play position is exactly the new value. A coarse pass (every 100 ms)
        finds when in the second it ticks; after that each tick is predictable and
        only needs fine polling (every 10 ms) around it. ~10 requests per sample
        instead of ~50. Returns the median of `samples` ticks."""
        sp = self._coordinator()
        client = self.broadcaster.newest(sp.ip_address)
        if client is None:
            return None

        def wait_tick(start_value, deadline, step):
            while time.monotonic() < deadline:
                r = self._reltime(sp)
                if r != start_value:
                    return r, time.monotonic(), client.frames_sent
                time.sleep(step)
            return None

        first = wait_tick(self._reltime(sp), time.monotonic() + 1.3, 0.1)
        if first is None:
            return None  # not advancing (paused / buffering)
        value, t_tick = first[0], first[1]
        out = []
        for _ in range(samples):
            time.sleep(max(0.0, t_tick + 0.85 - time.monotonic()))  # wake just before the next tick
            tick = wait_tick(value, t_tick + 1.4, 0.01)
            if tick is None:
                break
            value, t_tick, sent = tick
            out.append(sent / OUT_RATE - value)
        if not out:
            return None
        out.sort()
        return out[len(out) // 2]

    def _latency_control(self):
        correcting = False
        while True:
            time.sleep(4 if correcting or self.latency is None else 15)
            if not self.casting:
                self.capture.rate_adj, self.latency, correcting = 0, None, False
                continue
            if self.sonos_paused:
                continue
            try:
                b = self.measure_latency(samples=3)
            except Exception:
                continue
            if b is None or not self.casting:
                continue
            self.latency = b
            lo, hi = self.LATENCY_BAND
            if not self.wav:  # MP3: the Sonos decides its own (big) buffer - just report it
                self.on_change()
                continue
            if b > hi or b < lo:
                correcting = True
            elif abs(b - self.LATENCY_TARGET) < self.LATENCY_SETTLE:
                correcting = False
            # +/-2 frames per 10 ms block = 0.4% rate change, ~4 ms of buffer per second
            self.capture.rate_adj = 0 if not correcting else (-2 if b > self.LATENCY_TARGET else 2)
            log.debug("sonos buffer %.3fs adj %+d", b, self.capture.rate_adj)
            self.on_change()

    SUSTAINED_S = 3.0  # this much continuous sound counts as real playback (games etc.)

    def _real_playback(self):
        """Is the PC actually playing something, rather than a notification ding?
        A media player (YouTube, Spotify, VLC...) playing audibly counts at once;
        anything else needs a few seconds of continuous sound."""
        if time.monotonic() - self.capture.sound_since >= self.SUSTAINED_S:
            return True
        return self.media.playing_audible()

    def _sonos_is_free(self):
        """Only auto-take-over a Sonos that isn't busy playing something else (TV, Spotify...)."""
        try:
            sp = self._coordinator()
            uri = sp.avTransport.GetMediaInfo([("InstanceID", 0)])["CurrentURI"]
            if uri.startswith("x-sonos-htastream:"):
                # On the TV input the transport state says nothing (it's "PLAYING"
                # forever). HTAudioIn is 0 when the TV isn't sending anything; any
                # other value -> someone may be watching TV, so don't take over.
                return sp.deviceProperties.GetZoneInfo().get("HTAudioIn", "1") == "0"
            state = sp.get_current_transport_info()["current_transport_state"]
            if state not in ("PLAYING", "TRANSITIONING"):
                return True
            return self.is_ours(uri)
        except Exception:
            return False


def main():
    ap = argparse.ArgumentParser(description="Cast all PC audio to Sonos")
    ap.add_argument("--sonos", help="speaker IP (default: from config)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--volume", type=int, help="set Sonos volume 0-100")
    ap.add_argument("--device", type=int, help="loopback device index (default: current output)")
    ap.add_argument("--keep-local", action="store_true",
                    help="don't mute the PC speakers or link the Windows volume slider to the Sonos")
    ap.add_argument("--stop", action="store_true", help="stop the Sonos and exit")
    ap.add_argument("--lag", action="store_true",
                    help="print how many ms the Sonos sound trails the picture (for players' A/V offset)")
    args = ap.parse_args()
    if args.lag:
        status = send_command("status", args.port) or {}
        print(status.get("av_delay_ms") or 170)  # typical WAV value if not measured yet
        return
    logging.basicConfig(level=logging.INFO, format="[sonos-cast] %(message)s")

    cfg = load_config()
    cfg["auto_cast"] = False  # CLI: cast now, stop on Ctrl+C
    if args.sonos:
        cfg["speaker_ip"] = args.sonos
    if args.keep_local:
        cfg["keep_local"] = True

    if args.stop:
        if send_command("stop", args.port) is None:
            sp = find_speaker(cfg["speaker_ip"], cfg["speaker_name"])
            if sp:
                sp.group.coordinator.stop()
        return

    try:
        caster = SonosCaster(cfg, port=args.port, device=args.device)
    except OSError:
        log.error("port %d is busy - the tray app is probably running (use its icon)", args.port)
        raise SystemExit(1)
    if not caster.start():
        raise SystemExit(1)
    if args.volume is not None:
        caster.set_volume(args.volume)
    log.info("stream: %s - Ctrl+C to stop", caster.stream_url)

    # Closing the console window skips `finally`; this makes it unmute the PC too.
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    def on_console_event(event):
        if event in (2, 5, 6):  # CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN
            caster.shutdown()
        return False
    ctypes.windll.kernel32.SetConsoleCtrlHandler(on_console_event, True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        caster.shutdown()


if __name__ == "__main__":
    main()
