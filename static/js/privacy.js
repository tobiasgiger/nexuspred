/* Privacy mode: mask account names on screen (first 6 characters, the rest as
   asterisks) — for screenshots, screen sharing and streaming. Per browser. */
const KEY = "np_privacy";
let on = false;
try { on = localStorage.getItem(KEY) === "1"; } catch (e) { /* ignore */ }

export const isPrivate = () => on;

export function setPrivate(value) {
  on = !!value;
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) { /* ignore */ }
  document.documentElement.classList.toggle("privacy", on);
}

/** "PAAPEX1739050000021" → "PAAPEX*************" while privacy mode is on. */
export function maskAccount(name) {
  const s = name == null ? "" : String(name);
  if (!on || !s) return s;
  const keep = 6;
  return s.slice(0, keep) + "*".repeat(Math.max(4, s.length - keep));
}

document.documentElement.classList.toggle("privacy", on);
