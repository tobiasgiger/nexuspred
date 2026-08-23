# Privacy Policy — Tradovate Bridge · Token Extractor Helper

_Last updated: 2026-08-23_

This extension is a personal helper that reads **your own** session tokens from a
tab **you** have open and logged in to, so you can copy them into your own
self-hosted "Tradovate Bridge".

## What it accesses

- When **you click the extension** on a `discord.com` or `tradovate.com` tab, it
  runs a read-only script in that tab to read authentication tokens from the
  page's own `localStorage` / `sessionStorage` (Discord user token; Tradovate
  `token` and `checkToken`).

## What it does with that data

- It **displays** the token(s) in the extension popup and, on your action, copies
  one to your **clipboard**.
- That's all. The value stays on your device.

## What it does NOT do

- It does **not** send tokens (or any other data) to any server. The extension
  makes **no network requests** of its own.
- It does **not** store tokens (no background storage, no sync, no history).
- It does **not** use analytics, tracking, cookies, or third-party services.
- It does **not** read any site other than `discord.com` and `tradovate.com`, and
  only when you explicitly invoke it.

## Data sharing

None. No data is transmitted, sold, or shared with anyone.

## Contact

This is an open, self-hosted tool. Questions: open an issue on the repository you
obtained it from.
