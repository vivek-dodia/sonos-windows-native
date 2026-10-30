# Sonos Cast for Windows

Play **all Windows audio on a Sonos speaker**: Chrome, YouTube, Netflix, VLC, Spotify, games, anything. The delay is about **0.15 s**, and video stays lip-synced.

It runs in the system tray. Play something on the PC and it comes out of the Sonos.

## Install

### Option 1: download the exe (no Python needed)

1. Download `SonosCast.exe` from the [latest release](../../releases/latest), or from the newest [Build run](../../actions)'s artifacts.
2. Put it somewhere permanent, such as `C:\Program Files\SonosCast\` or your Documents folder, and run it.
3. When Windows Firewall asks, click **Allow** on **private networks**. The Sonos pulls the audio from the PC on port 8080.

It finds your Sonos automatically and adds itself to the Start Menu and to "start with Windows". A tray icon appears; click it, or press **Ctrl+Alt+S**, to cast.

> Windows SmartScreen may warn about an unsigned app: click **More info → Run anyway**.

### Option 2: from source

```
git clone https://github.com/vivek-dodia/sonos-windows-native.git
cd sonos-windows-native
pip install -r requirements.txt
cast-to-sonos.bat
```

This needs Python 3.10+ on Windows 10 or 11.

### Lip-sync for Chrome video (one-time, optional)

Right-click the tray icon and choose **Sync Chrome video to the Sonos...**. It opens `chrome://extensions` and the extension folder for you:

1. Turn on **Developer mode**, at the top right.
2. Click **Load unpacked** and pick the folder.

While you're casting, video in Chrome is held back by the measured Sonos delay, so the picture lines up with the sound. The toolbar badge shows the delay. Click it for a fine-tune slider.

## Using it

| | |
|---|---|
| **Auto-cast** | Starts when the PC really plays something. A media player (YouTube, Spotify, VLC...) starts it right away; other sound, like games, after 3 s. Notification dings don't count. Stops after 5 minutes of quiet. It never takes over a Sonos that's busy with something else, such as Spotify on your phone or a TV that's sending sound. |
| **Tray icon / `Ctrl+Alt+S`** | Start or stop casting by hand. |
| **Sonos play/pause** | The Playbar's touch button or the Sonos app pauses and resumes what's playing on the PC, through Windows media controls. |
| **Volume keys** | Control the Sonos volume, both ways. The mute key mutes the Sonos. |
| **PC speakers** | Muted while casting and restored afterwards, even after a crash or shutdown. |
| **Headphones** | Connecting headphones (AirPods, Bluetooth, a headset) stops casting. Your audio stays private. |
| **Sonos app** | Shows what's playing, such as the YouTube video title. |
| **Icon colour** | Grey: idle. Green: casting. Amber: no PC audio device (monitor asleep?). Red: can't reach the Sonos. |
| **Right-click menu** | Volume, choose a speaker, auto-cast, PC speaker muting, start with Windows, play a test sound, Chrome setup, log. |

**Local videos:** drag a file onto `play-synced.bat`. VLC plays the audio early by the measured delay.

Settings and the log are in `%APPDATA%\SonosCast`.

## How it works

```
 Windows audio ──► WASAPI loopback ──► pacer (10 ms blocks, drift control) ──► HTTP /stream.wav ──► Sonos
 (any app)         (before Windows'      + inaudible noise floor in silence       (raw 48 kHz PCM)     pulls it
                    volume/mute)                                                                         over the LAN
                                                    ▲
         latency controller: measures how much audio the Sonos holds ──┘ (nudges the rate ±0.4%)
                                                    │
         Chrome extension / VLC ◄── /status (av_delay_ms) ── hold the picture back by exactly that much
```

- **Capture:** [WASAPI loopback](https://github.com/s0d3s/PyAudioWPatch) records whatever Windows is playing, before the endpoint volume and mute are applied. That's why the PC can be muted while the Sonos still gets full-level audio.
- **Why WAV:** the Sonos pre-buffers a fixed number of *bytes* before it plays. With MP3 that's 3–6 s of audio; with raw PCM at 1.5 Mbit/s it's about 0.12 s.
- **Never silent:** pure digital silence makes the Sonos spend about 10 s probing the stream. Gaps are padded with ±2 LSB noise (about −80 dB, inaudible), so the Sonos starts in about 0.2 s.
- **Drift control:** the PC's, the sound card's and the Sonos's clocks all differ slightly. Two tiny rate adjustments (±2 samples per 10 ms block) keep about 15 ms queued on the PC and about 0.13 s in the Sonos. The Sonos buffer is measured by sampling its play position the moment it ticks over. Below about 0.08 s it underruns, which is why zero isn't possible over a network.
- **Control:** [SoCo](https://github.com/SoCo/SoCo) (UPnP) points the Sonos at the stream and handles volume and transport events. Windows' media session API pauses and resumes apps.
- **Lip-sync:** the Chrome extension copies each decoded frame (`requestVideoFrameCallback` + `createImageBitmap`, which stays on the GPU). An overlay canvas shows each frame `delay` ms later. The real video keeps playing underneath, and the site's controls stay usable. DRM video (Netflix, F1 TV...) can't be copied, so there the picture leads by about 0.15 s; the badge shows **DRM**.

Measured on a Playbar: 83–147 ms Sonos buffer over a 30-minute soak test, with no drift. The Chrome overlay is within one frame of target, with 0 dropped frames at 60 fps.

## Troubleshooting

- **The Sonos never plays:** allow the app through Windows Firewall on private networks (port 8080, TCP inbound). As admin:
  `netsh advfirewall firewall add rule name="Sonos Cast" dir=in action=allow protocol=tcp localport=8080`
- **Wrong speaker:** right-click → Speaker → Search for speakers.
- **Lips slightly off:** use the slider in the extension popup. For VLC, edit `EXTRA_MS` in `play-synced.bat`.
- **Weak Wi-Fi:** set `"format": "mp3"` in `%APPDATA%\SonosCast\config.json`. That uses 5x less bandwidth, at about 3.7 s of delay.
- Anything else: right-click → **Open log**.

## Development

| File | |
|---|---|
| `sonos_tray.py` | Tray app (entry point of the exe) |
| `sonos_cast.py` | Engine: capture, pacing, stream server, Sonos control, latency controller. Also a console version: `python sonos_cast.py --volume 30`, `--stop`, `--lag` |
| `chrome-extension/` | Sonos Sync lip-sync extension (MV3) |
| `play-synced.bat` | VLC with the audio advanced by the live delay |
| `SonosCast.spec` | PyInstaller build: `pip install pyinstaller` and then `pyinstaller SonosCast.spec`, which writes `dist\SonosCast.exe` |
| `.github/workflows/build.yml` | CI: test, build the exe, self-test it, upload it, and attach it to a release on `v*` tags |

Local control API (127.0.0.1 only): `http://127.0.0.1:8080/toggle | start | stop | status`.

### Tests

```
python tests/test_pacing.py      # no hardware: drift control, no gaps, no distortion (runs in CI)
python tests/test_live.py        # real Sonos: startup, latency, controller, events, auto-cast, MP3 fallback
python tests/test_local.py       # PC side: mute, volume link, mute key, real audio, tray menu
python tests/test_behaviour.py   # auto-cast triggers, headphones hand-back, Sonos title
python tests/test_media.py <dir with media.html+test.mp4>                     # Sonos play/pause <-> Chrome
python tests/test_extension.py <dir with index.html+test.mp4> [youtube-url]   # extension (tray casting)
```

The hardware tests take over the Sonos. Quit the tray app first, except for `test_extension`, which needs it running and casting. The extension test needs Playwright (`pip install playwright` and then `python -m playwright install chromium`) and a test video with an audio track:
`ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=60:duration=30 -f lavfi -i sine=frequency=440:duration=30 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest test.mp4`

To make a release, push a tag (`git tag v1.0.0 && git push --tags`), and CI attaches the exe and the extension zip.
