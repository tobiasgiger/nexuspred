/* Localisation. English is the source language: every user-visible string is
   written in English in the code and passed through t(); the German dictionary
   (locales/de.js) maps the English text to its translation. A missing entry
   falls back to English, so a new string never breaks the page.

   Language = the "Language" setting (Settings → General): "auto" follows the
   browser, "de" / "en" force one. The resolved choice is cached in localStorage
   (no flash of English before the settings arrive) and mirrored in the fb_lang
   cookie so the server-rendered sign-in pages speak the same language. */
import { DE } from "./locales/de.js";

const DICTS = { de: DE };
export const LANGUAGES = [["auto", "Browser default"], ["de", "Deutsch"], ["en", "English"]];
const PREF_KEY = "fb_lang_pref";

function browserLang() {
  const cands = (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || "en"]);
  for (const c of cands) if (String(c).toLowerCase().startsWith("de")) return "de";
  return "en";
}

function resolve(pref) {
  return pref === "de" || pref === "en" ? pref : browserLang();
}

let pref = "auto";
try { pref = localStorage.getItem(PREF_KEY) || "auto"; } catch (e) { /* ignore */ }
let current = resolve(pref);
apply(current);

function apply(l) {
  try { document.documentElement.lang = l; } catch (e) { /* ignore */ }
  try { document.cookie = `fb_lang=${l}; path=/; max-age=31536000; SameSite=Lax`; } catch (e) { /* ignore */ }
}

/** The active language code: "de" | "en". */
export function lang() { return current; }

/** The preference as stored: "auto" | "de" | "en". */
export function preference() { return pref; }

/** BCP-47 locale for number / date formatting — [] keeps the browser's own choice for English. */
export function locale() { return current === "de" ? "de-CH" : []; }

/** Translate an English source string; {name} placeholders are filled from params. */
export function t(text, params) {
  const dict = DICTS[current];
  let s = (dict && Object.prototype.hasOwnProperty.call(dict, text)) ? dict[text] : text;
  if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
  return s;
}

/** Adopt the setting from the server (called once the settings are loaded).
    Returns true when the resolved language changed — the caller reloads. */
export function adopt(settingPref) {
  const p = settingPref === "de" || settingPref === "en" ? settingPref : "auto";
  if (p !== pref) {
    pref = p;
    try { localStorage.setItem(PREF_KEY, p); } catch (e) { /* ignore */ }
  }
  const next = resolve(p);
  if (next !== current) { current = next; apply(current); return true; }
  return false;
}
