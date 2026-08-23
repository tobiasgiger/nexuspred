# My Discord Token — nexuspred helper (Chrome/Edge extension)

A tiny Manifest V3 extension that reads **your own** Discord **user token** from a
logged-in `discord.com` browser tab and shows it to you, so you don't have to dig
through DevTools / the Network tab to get the token for the bridge's
**Discord Signals** module.

## What it does — and doesn't

- Reads your token from the open `discord.com` tab's `localStorage` (with a
  webpack fallback) and displays it in the popup, masked by default.
- **100% local. It makes no network requests and sends the token nowhere.** It's
  the same thing you'd type into DevTools (`localStorage.token`), behind a button.
- Only reads your own logged-in session. It can't read anyone else's account.

## ⚠️ Security

A Discord **user token is equivalent to your account password** — anyone who gets
it has full access to your account (and using a personal token for automation is
against Discord's ToS, which you've already accepted for this setup).

- Never share it, screenshot it, or paste it into any site other than **your own
  bridge** (Discord Signals → *Discord user token*).
- The bridge stores it masked in `data/settings.json` and never returns it to the
  browser in plain text.
- If you ever suspect it leaked: change your Discord password — that invalidates
  all existing tokens.

## Install (unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this folder
   (`browser-extension/discord-token/`).
4. Pin the extension if you like (puzzle-piece icon → pin).

## Use

1. Open <https://discord.com/app> in a tab and make sure you're **logged in**.
2. Click the extension icon.
3. Click **Get my token** (it also runs automatically when the popup opens).
4. **Copy**, then paste into the bridge's **Discord Signals → user token** field
   and **Save**.

## Notes / troubleshooting

- **"Open discord.com … first"** — the active tab isn't a discord.com page. Switch
  to your Discord tab and click the icon again.
- **"No token found"** — you're not logged in in that tab, or Discord changed its
  internals. Log in (or reload the tab) and retry. The webpack fallback can break
  after Discord updates; the `localStorage` path is the reliable one.
- Works in Chrome and Edge (Chromium, Manifest V3). It targets the **web** client
  (`discord.com`), not the desktop app.
- No icons are bundled, so Chrome shows a default puzzle-piece icon — that's
  cosmetic and doesn't affect behaviour.

## Permissions, and why

- `host_permissions: *://*.discord.com/*` — so it can read the token from your
  Discord tab.
- `scripting` + `activeTab` — to run the read-only extraction in the tab you're
  looking at, only when you click the extension.

No other permissions. No background page, no storage, no network.
