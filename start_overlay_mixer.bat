@echo off
setlocal
cd /d "%~dp0"

REM start_overlay_mixer_v2.0.2.bat
REM Version 2.0.2

REM Optional: make sure watchdog is installed
REM pip install watchdog

title Overlay Mixer
echo Starting Overlay Mixer Watchdog v2.0.2...
python overlay_mixer_watchdog.py

REM If the script has stopped, keep the window open:
echo.
echo (Stopped. Press any key to close.)
pause >nul
