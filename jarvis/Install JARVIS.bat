@echo off
REM Double-click this on Windows to set up JARVIS.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install it from https://python.org, tick
  echo "Add Python to PATH" during setup, then run this again.
  pause
  exit /b 1
)
python install.py
echo.
pause
