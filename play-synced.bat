@echo off
REM Drag a video onto this while casting to the Sonos.
REM VLC plays the audio early by the Sonos' measured delay, so sound and picture line up on the Sonos.
REM Lips still off? Add a correction in ms: audio late -> positive, audio early -> negative.
set EXTRA_MS=0
set LAG_MS=170
for /f %%L in ('powershell -NoProfile -Command "try { (Invoke-RestMethod http://127.0.0.1:8080/status -TimeoutSec 2).av_delay_ms } catch {}"') do set LAG_MS=%%L
set /a LAG_MS=%LAG_MS%+%EXTRA_MS%
set VLC="C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"
if not exist %VLC% set VLC="C:\Program Files\VideoLAN\VLC\vlc.exe"
start "" %VLC% --audio-desync=-%LAG_MS% "%~1"
