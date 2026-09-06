/* Hash router with deep links: #/webhooks/wh_123, #/settings/alerts?tab=x */

export function parseHash() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, qs = ""] = raw.split("?");
  return { path: path.startsWith("/") ? path : "/" + path, query: new URLSearchParams(qs) };
}

export function matchRoute(routes, path) {
  const parts = path.split("/").filter(Boolean);
  for (const route of routes) {
    const rp = route.path.split("/").filter(Boolean);
    if (rp.length !== parts.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < rp.length; i++) {
      if (rp[i].startsWith(":")) params[rp[i].slice(1)] = decodeURIComponent(parts[i]);
      else if (rp[i] !== parts[i]) { ok = false; break; }
    }
    if (ok) return { route, params };
  }
  return null;
}

export function navigate(path, { replace = false } = {}) {
  const target = "#" + path;
  if (window.location.hash === target) return;
  if (replace) window.history.replaceState(null, "", target);
  else window.location.hash = path;
  if (replace) window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function currentPath() {
  return parseHash().path;
}
