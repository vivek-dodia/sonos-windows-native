@echo off
REM Starts the Sonos Cast tray app (no console). If it's already running, this toggles casting.
REM For a console/debug version run:  python sonos_cast.py
cd /d "%~dp0"
start "" pythonw sonos_tray.py
