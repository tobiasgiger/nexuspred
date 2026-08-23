# Tradovate Bridge — Token Extractor Helper (Chrome/Edge extension)

A tiny Manifest V3 extension that reads **your own** tokens from a logged-in
browser tab, so you don't have to dig through DevTools / the Network tab to get
the values the bridge needs:

- **Discord** (`discord.com`) — your **user token** for the Discord Signals module.
- **Tradovate** (`tradovate.com`) — your session **`token`** and **`checkToken`**
  (plus any other token-ish keys it finds) for Settings → Token Accounts.

## What it does — and doesn't

- Reads tokens from the active tab's `localStorage` / `sessionStorage` (with a
  webpack fallback for Discord) and shows them in the popup, masked by default.
- **100% local. It makes no network requests and sends the tokens nowhere.** It's
  the same thing you'd read in DevTools, behind a button.
- Only reads your own logged-in session. It can't read anyone else's account.

## ⚠️ Security

Each token is **equivalent to a password** for that account — anyone who gets it
has full access. (Using a personal Discord token for automation also breaks
Discord's ToS, which you've already accepted for this setup.)

- Never share a token, screenshot it, or paste it into any site other than **your
  own bridge**.
- The bridge stores tokens masked in `data/settings.json` and never returns them
  to the browser in plain text.
- If one leaks: change that account's password — it invalidates existing tokens.

## Install (unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this folder
   (`browser-extension/token-extractor/`).
4. Pin the extension if you like (puzzle-piece icon → pin).

## Use

Open the relevant site **logged in**, then click the extension:

- **Extract from active tab** — auto-detects Discord vs Tradovate from the tab.
- **Discord** / **Tradovate** — force a specific extractor.

Then **Reveal**/**Copy** each token and paste it into the bridge:

| Token | Where it comes from | Where it goes in the bridge |
|---|---|---|
| Discord user token | `discord.com` tab | Settings → Discord Listener → *Discord user token* |
| Tradovate `token` | `tradovate.com` web trader | Settings → Token Accounts → *access token* |
| Tradovate `checkToken` | `tradovate.com` web trader | Settings → Token Accounts (renewal token) |

> Tradovate key names can change between releases; the extractor also lists **any**
> storage key containing "token" so nothing is missed — match them up by name.

## Troubleshooting

- **"Open a discord.com or tradovate.com tab…"** — the active tab isn't a
  supported site. Switch to the right tab and click again.
- **"No token found"** — you're not logged in in that tab (or Discord changed its
  internals). Log in / reload the tab and retry. For Discord the `localStorage`
  path is reliable; the webpack fallback can break after Discord updates.
- Works in Chrome and Edge (Chromium, Manifest V3). It targets the **web** clients
  (`discord.com`, `tradovate.com`), not the desktop apps.
- No icons are bundled, so Chrome shows a default puzzle-piece icon — cosmetic
  only.

## Permissions, and why

- `host_permissions: *://*.discord.com/*`, `*://*.tradovate.com/*` — to read
  tokens from those tabs.
- `scripting` + `activeTab` — to run the read-only extraction in the tab you're
  looking at, only when you click the extension.

No other permissions. No background page, no storage, no network.
