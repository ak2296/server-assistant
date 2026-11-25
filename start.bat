@echo off
cd /d "%~dp0"
if not exist ".venv" (
    echo First run: setting up...
    py -3 -m venv .venv 2>nul || python -m venv .venv || (echo Python 3.11 or newer is needed: https://www.python.org/downloads/ & pause & exit /b)
)
.venv\Scripts\python -m pip install -q -r requirements.txt
rem Optional: lets the app open in its own window (skipped quietly if it can't be installed)
for /f "usebackq delims=" %%p in ("requirements-optional.txt") do .venv\Scripts\python -m pip install -q "%%p" >nul 2>&1
if not exist "config.toml" copy config.example.toml config.toml >nul
rem Runs in the background with an icon in the Windows tray (by the clock).
start "" ".venv\Scripts\pythonw.exe" tray.py --open
echo Server Assistant is running - look for its icon in the tray by the clock.
timeout /t 4 >nul
