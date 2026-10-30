# PyInstaller build for SonosCast.exe (one file, no console):  pyinstaller SonosCast.spec
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

hidden = (
    ["pystray._win32", "pycaw.pycaw", "comtypes.stream"]
    + collect_submodules("winrt.windows.media.control")
    + collect_submodules("winrt.windows.foundation")
    + collect_submodules("winrt.system")
)

a = Analysis(
    ["sonos_tray.py"],
    datas=[("chrome-extension", "chrome-extension")],
    binaries=collect_dynamic_libs("pyaudiowpatch"),
    hiddenimports=hidden,
    excludes=["tkinter", "playwright", "matplotlib", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="SonosCast",
    icon="assets/icon.ico",
    console=False,
    upx=False,
)
