/* JSON API client. A 401 means the session expired → back to the login page. */

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

function messageOf(data, fallback) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length && d[0].msg) return d[0].msg;
  if (data && typeof data.message === "string") return data.message;
  return fallback || "Request failed";
}

async function request(method, path, body) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (res.status === 401) {
    window.location.href = "/login";
    throw new ApiError("Signed out", 401, data);
  }
  if (!res.ok) throw new ApiError(messageOf(data, res.statusText), res.status, data);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};
