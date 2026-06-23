@echo off
REM ===========================================================================
REM  Connect an existing (ZIP-downloaded) nexuspred folder to GitHub so the
REM  dashboard "Update" button works. Run this once, in the install folder.
REM
REM  Your settings (data\settings.json) are git-ignored and are NOT touched.
REM  Code files are reset to match the repo's latest main branch.
REM ===========================================================================
setlocal
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
  echo  [X] Git is not installed. Get it from https://git-scm.com/download/win
  echo      then run this script again.
  pause
  exit /b 1
)

echo  ==^> Connecting this folder to github.com/tobiasgiger/nexuspred ...
if not exist ".git" git init
git remote remove origin >nul 2>&1
git remote add origin https://github.com/tobiasgiger/nexuspred.git
git fetch origin
if errorlevel 1 (
  echo  [X] git fetch failed. Is the repository public, or are you signed in to Git?
  pause
  exit /b 1
)
git reset --hard origin/main
git branch -M main
git branch --set-upstream-to=origin/main main

echo.
echo  [ok] Connected. Restart the bridge (start.bat); the Update button now works.
echo.
pause
