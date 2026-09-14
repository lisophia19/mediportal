// Thin fetch wrapper around the Flask dashboard API (spec §5.8).
// Assumption: the backend is mounted under /api/v1 and issues a bearer JWT
// from POST /auth/login. Double-check these shapes against the real routes
// once backend/ exists.

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const TOKEN_KEY = "mediportal_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

async function request(path, options = {}) {
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  };

  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      message = body.message || body.error || message;
    } catch {
      // response had no JSON body; keep the generic message
    }
    throw new Error(message);
  }

  if (response.status === 204) return null;
  return response.json();
}

// Assumption: login returns { token: "<jwt>" }. Confirm against spec §5.8.
export function login(email, password) {
  return request("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function logout() {
  return request("/auth/logout", { method: "POST" }).finally(clearToken);
}

// Assumption: paginated list shape is
// { calls: [...], page, total_pages } — confirm against spec §5.8.
export function fetchCalls({ status, q, page } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  if (page) params.set("page", page);
  const query = params.toString();
  return request(`/calls${query ? `?${query}` : ""}`);
}

export function fetchCallDetail(id) {
  return request(`/calls/${id}`);
}
