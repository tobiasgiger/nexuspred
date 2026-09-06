/* Dark / light theme, persisted per browser; "system" follows the OS. */
const KEY = "fb_theme";

export function getTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(KEY, theme); } catch (e) { /* ignore */ }
}

export function initTheme() {
  let pref = null;
  try { pref = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const apply = () => { document.documentElement.dataset.theme = mq.matches ? "light" : "dark"; };
  if (!pref || pref === "system") {
    apply();
    mq.addEventListener("change", () => { try { if (!localStorage.getItem(KEY) || localStorage.getItem(KEY) === "system") apply(); } catch (e) { apply(); } });
  } else {
    document.documentElement.dataset.theme = pref;
  }
}
