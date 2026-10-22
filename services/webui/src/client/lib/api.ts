import axios from 'axios';

const CSRF_COOKIE_NAME = 'gough_csrf';
const CSRF_HEADER_NAME = 'X-CSRF-Token';

/**
 * Reads the `gough_csrf` cookie set by the backend on login. This cookie is
 * intentionally NOT HttpOnly (unlike `gough_access`/`gough_refresh`) so the
 * client can echo its value back on state-changing requests. The access and
 * refresh tokens themselves are HttpOnly and never touched by JS.
 */
export function getCsrfToken(): string | null {
  const prefix = `${CSRF_COOKIE_NAME}=`;
  const cookies = document.cookie.split(';');
  for (const cookie of cookies) {
    const trimmed = cookie.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.substring(prefix.length));
    }
  }
  return null;
}

const api = axios.create({
  baseURL: '/api/v1',
  // Send the HttpOnly gough_access / gough_refresh cookies automatically on
  // every request instead of attaching a bearer token read from storage.
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Attach the CSRF token to every state-changing request. GET/HEAD requests
// are exempt per the backend contract.
api.interceptors.request.use((config) => {
  const method = (config.method ?? 'get').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    const csrfToken = getCsrfToken();
    if (csrfToken) {
      config.headers[CSRF_HEADER_NAME] = csrfToken;
    }
  }
  return config;
});

// 401s mean the session cookie is missing/expired. There is nothing to clear
// client-side (the cookies are HttpOnly); callers relying on `isAuthenticated`
// (e.g. ProtectedRoute) pick this up on their next checkAuth() call.
api.interceptors.response.use(
  (response) => response,
  (error) => Promise.reject(error)
);

export default api;
