@echo off
setlocal
cd /d "%~dp0"

REM Runs all tests. Works only in temporary folders; the configured folders,
REM overlay_mixer.ini and the production log are never touched.
REM
REM Without a parameter the newest script from the version folders (vX.Y.Z) is
REM tested, or overlay_mixer_watchdog.py if there are no version folders.
REM With a parameter a specific script, e.g. the file currently in production:
REM   run_tests.bat overlay_mixer_watchdog.py
if not "%~1"=="" set "VAH_MIXER_SCRIPT=%~1"

python -B -m unittest discover -s tests -v

echo.
pause
