"""
Sonos Cast tray app - PC audio on the Sonos, no console window.

    pythonw sonos_tray.py

- Left-click the tray icon (or press Ctrl+Alt+S) to start/stop casting.
- Auto-cast: starts when the PC plays sound, stops after it's been quiet a while.
  It won't take over the Sonos while it's busy with something else (TV, Spotify...).
- While casting the PC speakers are muted and the volume keys / slider (and the
  mute key) control the Sonos.
- Launching it again while it's running just toggles casting.

Icon colour: grey = idle, green = casting, amber = casting but the PC has no
audio output (monitor asleep?), red = can't reach the Sonos.
"""
import ctypes
import ctypes.wintypes
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

import pystray
import soco
from PIL import Image, ImageDraw

import sonos_cast
from sonos_cast import APP_DIR, PORT, SonosCaster, load_config, save_config, send_command

log = logging.getLogger("sonos-cast")

APP_NAME = "Sonos Cast"
FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller-built SonosCast.exe
HERE = Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))  # bundled data files (the exe unpacks to a temp dir)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOG_FILE = APP_DIR / "sonos-cast.log"
ICON_FILE = APP_DIR / "sonos-cast.ico"
HOTKEY = ("Ctrl+Alt+S", 0x0002 | 0x0001 | 0x4000, ord("S"))  # MOD_CONTROL|MOD_ALT|MOD_NOREPEAT

COLORS = {
    "off": (107, 114, 128),
    "casting": (16, 185, 129),
    "paused": (16, 185, 129),
    "no-device": (245, 158, 11),
    "unreachable": (239, 68, 68),
}
STATUS = {
    "off": "Idle",
    "casting": "Casting",
    "paused": "Paused from the Sonos - press play on the Sonos or the PC",
    "no-device": "Casting - no PC audio device (monitor asleep?)",
    "unreachable": "Can't reach the Sonos",
}


def draw_icon(state, size=64):
    s = 256  # draw big, downsample = antialiasing
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, s - 8, s - 8), fill=COLORS[state] + (255,))
    white = (255, 255, 255, 255)
    d.rectangle((62, 102, 98, 154), fill=white)               # speaker body
    d.polygon([(98, 102), (140, 68), (140, 188), (98, 154)], fill=white)  # cone
    if state == "paused":
        d.rectangle((150, 96, 170, 160), fill=white)             # pause bars
        d.rectangle((184, 96, 204, 160), fill=white)
    elif state in ("casting", "no-device"):
        for r in (34, 62):                                     # sound waves
            d.arc((140 - r, 128 - r, 140 + r, 128 + r), -45, 45, fill=white, width=14)
    elif state == "unreachable":
        d.line((160, 100, 204, 156), fill=white, width=16)
        d.line((204, 100, 160, 156), fill=white, width=16)
    return img.resize((size, size), Image.LANCZOS)


# ---- Windows integration -------------------------------------------------

def launch_target():
    """(program, arguments) that start this app: the exe, or pythonw + this script."""
    if FROZEN:
        return Path(sys.executable), ""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return (pyw if pyw.exists() else Path(sys.executable)), f'"{HERE / "sonos_tray.py"}"'


def launch_command():
    exe, args = launch_target()
    return f'"{exe}" {args}'.strip()


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def ensure_shortcut():
    """Start Menu entry, so it's searchable from the Start button. Rewritten on every
    launch so it follows the app if it moves (or switches between script and exe)."""
    programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    lnk = programs / f"{APP_NAME}.lnk"
    exe, args = launch_target()
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath=$env:TARGET;$s.Arguments=[string]$env:ARGS;$s.WorkingDirectory=$env:WD;"
        "$s.IconLocation=$env:ICON;$s.Description='Play PC audio on the Sonos';$s.Save()"
    )
    env = dict(os.environ, LNK=str(lnk), TARGET=str(exe), ARGS=args, WD=str(exe.parent if FROZEN else HERE),
               ICON=str(exe if FROZEN else ICON_FILE))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)


def extension_dir():
    """Folder Chrome loads the Sonos Sync extension from. The exe unpacks its files to a
    temp dir that is deleted on exit, so it keeps a copy in %APPDATA%\\SonosCast
    (refreshed on every launch, so updating the exe updates the extension too)."""
    src = BUNDLE / "chrome-extension"
    if not FROZEN:
        return src
    dst = APP_DIR / "chrome-extension"
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except OSError as e:
        log.warning("can't update %s: %s", dst, e)
    return dst


def message_box(text, title=APP_NAME):
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def hotkey_loop(callback):
    """Global hotkey via RegisterHotKey; messages arrive on this thread's queue."""
    user32 = ctypes.windll.user32
    name, mods, vk = HOTKEY
    if not user32.RegisterHotKey(None, 1, mods, vk):
        log.warning("hotkey %s is taken by another app", name)
        return
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            threading.Thread(target=callback, daemon=True).start()


# ---- tray ----------------------------------------------------------------

class TrayApp:
    def __init__(self):
        self.cfg = load_config()
        self.speakers = []  # discovered, for the Speaker menu
        self.icon = None
        self.first_run = not sonos_cast.CONFIG_FILE.exists()
        self.caster = SonosCaster(self.cfg, on_change=self.refresh, notify=self.notify)
        self.icon = pystray.Icon(APP_NAME, draw_icon("off"), APP_NAME, self._menu())

    def _menu(self):
        I, S, M = pystray.MenuItem, pystray.Menu.SEPARATOR, pystray.Menu
        c = lambda: self.caster  # noqa: E731 - caster doesn't exist yet while building

        def vol_item(v):
            return I(f"{v}%", lambda: c().set_volume(v), radio=True,
                     checked=lambda _: c().volume is not None and abs(c().volume - v) < 5)

        return M(
            I(lambda _: "Stop casting" if c().casting else f"Cast to {c().speaker_name}",
              self.toggle, default=True),
            I(lambda _: self.status_line(), None, enabled=False),
            S,
            I("Volume", M(
                I("Mute", lambda: c().toggle_mute(), checked=lambda _: c().muted),
                S, *[vol_item(v) for v in range(100, 0, -10)])),
            I("Speaker", M(lambda: self._speaker_items())),
            S,
            I("Auto-cast when the PC plays sound", lambda: c().set_option("auto_cast", not self.cfg["auto_cast"]),
              checked=lambda _: self.cfg["auto_cast"]),
            I("Mute PC speakers while casting", lambda: c().set_option("keep_local", not self.cfg["keep_local"]),
              checked=lambda _: not self.cfg["keep_local"]),
            I("Start with Windows", lambda: (set_autostart(not autostart_enabled()), self.icon.update_menu()),
              checked=lambda _: autostart_enabled()),
            S,
            I("Play test sound", lambda: threading.Thread(target=self.test_sound, daemon=True).start()),
            I("Sync Chrome video to the Sonos...",
              lambda: threading.Thread(target=self.setup_chrome, daemon=True).start()),
            I("Open log", lambda: os.startfile(LOG_FILE)),
            I("Quit", self.quit),
        )

    def _speaker_items(self):
        I = pystray.MenuItem
        items = [I(sp.player_name, (lambda s: lambda: self.caster.choose_speaker(s))(sp), radio=True,
                   checked=(lambda s: lambda _: s.ip_address == self.cfg["speaker_ip"])(sp))
                 for sp in self.speakers]
        if not items:
            items = [I(self.caster.speaker_name, None, checked=lambda _: True, radio=True)]
        return items + [pystray.Menu.SEPARATOR, I("Search for speakers", self._discover)]

    def _discover(self):
        def run():
            found = sorted(soco.discover(timeout=4) or (), key=lambda s: s.player_name)
            self.speakers = found
            self.icon.update_menu()
        threading.Thread(target=run, daemon=True).start()

    def status_line(self):
        c = self.caster
        text = STATUS[c.state]
        if c.state == "off" and c.capture.headphones:
            text += " - headphones in use, audio stays in them"
        elif c.state == "off":
            text += " - auto-cast on" if self.cfg["auto_cast"] else " - click to cast"
        elif c.casting:
            if c.latency is not None:
                text += f" - {round(c.latency * 1000)} ms delay"
            if c.volume is not None:
                text += f" - volume {c.volume}%" + (" (muted)" if c.muted else "")
        return text

    def test_sound(self):
        """Casts (if needed) and plays a chime: you should hear it from the Sonos."""
        if not self.caster.casting and not self.caster.start():
            return
        time.sleep(0.8)
        import winsound
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS)

    def setup_chrome(self):
        """One-time: load the Sonos Sync extension (Chrome only allows unpacked
        extensions through its own UI)."""
        ext = extension_dir()
        chrome = next((p for p in (Path(os.environ.get(v, "")) / r"Google\Chrome\Application\chrome.exe"
                                   for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")) if p.exists()), None)
        subprocess.run(["explorer", f"/select,{ext / 'manifest.json'}"], check=False)
        if chrome:
            subprocess.Popen([str(chrome), "chrome://extensions"])
        message_box(
            "To sync Chrome video with the Sonos (one-time setup):\n\n"
            "1. In the Chrome tab that just opened, turn on 'Developer mode' (top right).\n"
            "2. Click 'Load unpacked' and pick this folder:\n"
            f"   {ext}\n"
            "   (Explorer has it open for you.)\n\n"
            "Done. Videos are then held back by the measured Sonos delay so the picture and the "
            "Sonos sound line up. Click the extension's icon to see the delay or fine-tune it.")

    def toggle(self):
        threading.Thread(target=self.caster.toggle, daemon=True).start()

    def notify(self, title, msg):
        log.info("%s: %s", title, msg)
        if self.icon is None:
            return
        try:
            self.icon.notify(msg, title)
        except Exception:
            pass

    def refresh(self):
        if self.icon is None:
            return
        state = self.caster.state
        self.icon.icon = draw_icon(state)
        self.icon.title = f"{APP_NAME} - {self.status_line()}"[:127]
        self.icon.update_menu()

    def quit(self):
        self.caster.shutdown()
        self.icon.stop()

    def run(self):
        def setup(icon):
            icon.visible = True
            self.refresh()
            save_config(self.cfg)
            if self.first_run:
                set_autostart(True)
                self.notify(APP_NAME, f"Running in the tray. Click the icon or press {HOTKEY[0]} to cast. "
                                      "Auto-cast is on: just play something.")
            self._discover()
        threading.Thread(target=hotkey_loop, args=(self.toggle,), daemon=True).start()
        self.icon.run(setup=setup)


def selftest(out):
    """`SonosCast.exe --selftest <file>`: check everything the exe needs is bundled
    (CI runs this on the built exe; there's no console, so results go to a file)."""
    import asyncio
    import pyaudiowpatch
    from pycaw.pycaw import AudioUtilities  # noqa: F401
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager  # noqa: F401
    checks = {
        "extension": (BUNDLE / "chrome-extension" / "manifest.json").is_file(),
        "portaudio": bool(pyaudiowpatch.get_portaudio_version_text()),
        "icon": draw_icon("casting").size == (64, 64),
        "asyncio": asyncio.run(asyncio.sleep(0, result=True)),
        "tray backend": pystray.Icon.__module__ == "pystray._win32",
    }
    lines = [f"{'ok  ' if v else 'FAIL'} {k}" for k, v in checks.items()]
    # info only: build machines have no sound devices or media sessions
    lines.append(f"info default output: {sonos_cast.default_output()}")
    lines.append(f"info now playing: {sonos_cast.MediaControl().now_playing()}")
    Path(out).write_text("\n".join(lines) + "\n")
    return all(checks.values())


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        try:
            ok = selftest(sys.argv[2])
        except Exception:  # an uncaught error in a windowed exe pops a dialog (and hangs CI)
            import traceback
            Path(sys.argv[2]).write_text("FAIL\n" + traceback.format_exc())
            ok = False
        sys.exit(0 if ok else 1)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # pythonw has no console: without these, a crash in any thread vanishes without a trace
    sys.excepthook = lambda *exc: log.critical("crash", exc_info=exc)
    threading.excepthook = lambda a: log.critical("crash in thread %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    if not ICON_FILE.exists():
        draw_icon("casting", 256).save(ICON_FILE, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])

    try:
        app = TrayApp()
    except OSError:
        # Port taken: either we're already running (-> toggle it) or something else has it.
        if send_command("toggle") is None:
            message_box(f"Port {PORT} is in use by another program.\nClose it and try again.")
        return
    threading.Thread(target=ensure_shortcut, daemon=True).start()
    extension_dir()
    if autostart_enabled():
        set_autostart(True)  # point it at wherever we run from now
    app.run()


if __name__ == "__main__":
    main()
