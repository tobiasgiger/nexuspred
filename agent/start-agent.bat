@echo off
rem Fluxbridge execution agent — Windows launcher.
rem First run asks for the bridge URL and the pairing code (Settings -> Execution Agents).
rem Needs Python 3.8+ (https://www.python.org/downloads/windows/, tick "Add python.exe to PATH").
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3 and tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)
:loop
python fluxbridge_agent.py %*
echo.
echo Agent exited (code %errorlevel%). Restarting in 10 seconds... press Ctrl+C to stop.
timeout /t 10 >nul
goto loop
