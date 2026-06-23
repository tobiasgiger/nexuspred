#!/usr/bin/env bash
#
# Connect an existing (ZIP-downloaded) nexuspred folder to GitHub so the
# dashboard "Update" button works. Run this once, in the install folder.
#
# Your settings (data/settings.json) are git-ignored and are NOT touched.
# Code files are reset to match the repo's latest main branch.
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v git >/dev/null 2>&1; then
  echo "Git is not installed. Install it and run this script again."
  exit 1
fi

echo "==> Connecting this folder to github.com/tobiasgiger/nexuspred ..."
[ -d .git ] || git init
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/tobiasgiger/nexuspred.git
git fetch origin
git reset --hard origin/main
git branch -M main
git branch --set-upstream-to=origin/main main

echo
echo "Connected. Restart the bridge (./start.sh); the Update button now works."
