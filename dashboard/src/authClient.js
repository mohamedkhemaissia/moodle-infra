export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function apiFetch(path, options = {}, fetcher = globalThis.fetch) {
  if (typeof path !== 'string' || !path.startsWith('/api/v1/')) throw new Error('Chemin API invalide.');
  const response = await fetcher(path, { ...options, credentials: 'same-origin', cache: 'no-store' });
  if (response.status === 401 && path !== '/api/v1/auth/login' && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('pilotage-session-expired'));
  }
  return response;
}

export async function jsonRequest(path, { method = 'GET', body, csrfToken, signal } = {}, fetcher) {
  const headers = { Accept: 'application/json' };
  const options = { method, signal, headers };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    options.body = JSON.stringify(body ?? {});
  }
  const response = await apiFetch(path, options, fetcher);
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(typeof data?.detail === 'string' ? data.detail
    : 'Le service est indisponible. Réessayez.', response.status);
  return data;
}

export function validateUser(value) {
  if (!value || !Number.isSafeInteger(value.id) || value.id < 1
      || typeof value.username !== 'string' || !/^[a-z][a-z0-9_.-]{2,31}$/.test(value.username)
      || typeof value.display_name !== 'string' || !value.display_name.length
      || !['admin', 'reader'].includes(value.role) || typeof value.active !== 'boolean') {
    throw new Error('Compte reçu invalide.');
  }
  return { id: value.id, username: value.username, display_name: value.display_name,
    role: value.role, active: value.active };
}

export function validateSession(value) {
  if (value?.enabled === false && value.user === null) return { enabled: false, user: null, csrfToken: null };
  const user = validateUser(value?.user);
  if (value.enabled !== true || !user.active || !/^[0-9a-f]{64}$/.test(value.csrf_token ?? '')) {
    throw new Error('Session reçue invalide.');
  }
  return { enabled: true, user, csrfToken: value.csrf_token };
}

export function initials(user) {
  return user.display_name.trim().split(/\s+/).slice(0, 2).map((word) => [...word][0]).join('').toLocaleUpperCase('fr-FR');
}

export function canManageUsers(user) { return user?.active === true && user.role === 'admin'; }
