@echo off
rem Same as start.bat but in a visible window (useful for seeing errors). Close the window to stop.
cd /d "%~dp0"
if not exist ".venv" python -m venv .venv
.venv\Scripts\python -m pip install -q -r requirements.txt
if not exist "config.toml" copy config.example.toml config.toml >nul
.venv\Scripts\python run.py
pause
