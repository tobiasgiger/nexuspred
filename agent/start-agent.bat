@echo off
rem Fluxbridge execution agent — Windows launcher (restarts on exit).
rem Preconfigured download: agent.json is already here, nothing to type.
rem Otherwise the first run asks for the bridge URL and a pairing code (Settings -> Execution Agents).
cd /d "%~dp0"
:loop
if exist fluxbridge-agent.exe (
  fluxbridge-agent.exe %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found and no fluxbridge-agent.exe is present.
    echo Install Python 3 ^(tick "Add python.exe to PATH"^) or download the preconfigured agent from the bridge.
    pause
    exit /b 1
  )
  python fluxbridge_agent.py %*
)
echo.
echo Agent exited (code %errorlevel%). Restarting in 10 seconds... press Ctrl+C to stop.
timeout /t 10 >nul
goto loop
