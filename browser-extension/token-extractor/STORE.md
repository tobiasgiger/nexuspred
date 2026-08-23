# Publishing to the Chrome Web Store (Unlisted) — checklist

> **Reality check (read first).** This extension's core function is **extracting
> authentication tokens** from `discord.com` / `tradovate.com`, and the Discord
> path enables a Discord-ToS-violating self-bot. Chrome Web Store review is
> **likely to reject** this under the malware/deceptive-behavior and
> single-purpose policies — even as **Unlisted**. Everything below maximizes the
> odds and gives you a link to try, but there's no guarantee it passes. The
> reliable, review-free paths remain **Load unpacked** (see README) or the
> in-app **Download (.zip)** button on the bridge's Tools tab.

## What's already prepared

- `manifest.json` — MV3, minimal permissions (`scripting`, `activeTab`), host
  permissions limited to `*.discord.com` and `*.tradovate.com`, plus `icons` +
  `action.default_icon`.
- `icons/icon16.png`, `icon48.png`, `icon128.png`.
- `PRIVACY.md` — a privacy policy (host it somewhere public and use that URL).

## Steps

1. **Developer account** — register at
   <https://chrome.google.com/webstore/devconsole> (one-time $5 USD fee).
2. **Package** — zip the **contents** of this folder (must include `manifest.json`
   at the zip root, `popup.html`, `popup.js`, `icons/`). The `.md` files may be
   included or omitted. The bridge's `GET /api/extension/token-extractor.zip`
   produces a folder-wrapped zip for Load-unpacked; for the store, upload a zip
   whose **root** is `manifest.json`.
3. **New item** → upload the zip.
4. **Store listing**
   - **Category**: Developer Tools (or Productivity).
   - **Summary / description**: see below.
   - **Screenshots**: at least one 1280×800 (or 640×400) — a shot of the popup.
   - **Icon**: 128×128 (already in `icons/`).
5. **Privacy tab**
   - **Single purpose**: "Display the signed-in user's own session tokens from
     the open Discord/Tradovate tab so they can be copied into the user's own
     self-hosted trading bridge."
   - **Permission justifications**:
     - `activeTab` + `scripting` — "Run a read-only extraction in the tab the user
       explicitly clicks the extension on; no persistent content scripts."
     - host `*.discord.com`, `*.tradovate.com` — "Read the token from these two
       sites' own storage; the extension does nothing on any other site."
   - **Data usage**: check **does NOT collect or transmit** user data; the token
     never leaves the device. Provide the **privacy policy URL** (host `PRIVACY.md`).
6. **Visibility**: **Unlisted** (installable via link, hidden from search).
7. **Submit for review.** If rejected, read the policy citation — for a token
   extractor it's usually not fixable by edits; fall back to Load unpacked.

## Suggested listing copy

**Summary (≤132 chars):**
> Copy your own Discord & Tradovate session tokens from the open tab into your self-hosted trading bridge. Local only.

**Description:**
> A personal helper for users of the self-hosted "Tradovate Bridge". On a tab you
> are logged in to, it reads your own session tokens (Discord user token;
> Tradovate token + checkToken) and lets you copy them into your own bridge's
> settings — so you don't have to use browser DevTools. It runs entirely on your
> device, makes no network requests, stores nothing, and only acts on
> discord.com / tradovate.com when you click it.

## Edge Add-ons

Microsoft Edge has an equivalent (free) at
<https://partner.microsoft.com/dashboard/microsoftedge> — same package, same
caveats.
