import test from 'node:test';
import assert from 'node:assert/strict';
import { apiFetch, jsonRequest, validateSession, validateUser, initials, canManageUsers } from '../src/authClient.js';

const user = { id: 1, username: 'hamma', display_name: 'Mohamed Khemaissia', role: 'admin', active: true };
const csrf = 'b'.repeat(64);

test('Session et droits utilisent des comptes valides sans conserver les champs secrets', () => {
  const session = validateSession({ enabled: true, user: { ...user, password_hash: 'private' }, csrf_token: csrf });
  assert.equal(session.user.display_name, user.display_name);
  assert.equal(session.user.password_hash, undefined);
  assert.equal(initials(session.user), 'MK');
  assert.equal(canManageUsers(session.user), true);
  assert.equal(canManageUsers({ ...user, role: 'reader' }), false);
  assert.equal(canManageUsers({ ...user, active: false }), false);
  for (const value of [null, { ...user, id: 0 }, { ...user, role: 'superuser' }, { ...user, active: 'true' }]) {
    assert.throws(() => validateUser(value));
  }
  assert.throws(() => validateSession({ enabled: true, user: { ...user, active: false }, csrf_token: csrf }));
  assert.throws(() => validateSession({ enabled: true, user, csrf_token: 'broken' }));
});

test('Les ecritures portent le cookie de meme origine et le jeton CSRF sans stockage navigateur', async () => {
  let captured;
  const fetcher = async (path, options) => { captured = { path, options }; return new Response(JSON.stringify(user)); };
  const data = await jsonRequest('/api/v1/users', { method: 'POST', csrfToken: csrf, body: { username: 'ahmed' } }, fetcher);
  assert.equal(data.username, 'hamma');
  assert.equal(captured.options.credentials, 'same-origin');
  assert.equal(captured.options.cache, 'no-store');
  assert.equal(captured.options.headers['X-CSRF-Token'], csrf);
  assert.equal(captured.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(captured.options.body), { username: 'ahmed' });
  await assert.rejects(apiFetch('https://other.example/api/v1/users', {}, fetcher));
});

test('Une session expiree previent le tableau de bord ; un echec de connexion conserve le formulaire', async () => {
  const previous = globalThis.window;
  const events = [];
  globalThis.window = { dispatchEvent: (event) => events.push(event.type) };
  try {
    const fetcher = async () => new Response(JSON.stringify({ detail: 'Session expiree.' }), { status: 401 });
    await assert.rejects(jsonRequest('/api/v1/incidents', {}, fetcher), { status: 401 });
    assert.deepEqual(events, ['pilotage-session-expired']);
    await assert.rejects(jsonRequest('/api/v1/auth/login', { method: 'POST', body: {} }, fetcher), { status: 401 });
    assert.equal(events.length, 1);
  } finally { globalThis.window = previous; }
});

test('Une reponse non JSON ou un refus API ne sont pas convertis en connexion reussie', async () => {
  await assert.rejects(jsonRequest('/api/v1/users', {}, async () => new Response('unavailable', { status: 503 })), { status: 503 });
  const data = await jsonRequest('/api/v1/auth/session', {}, async () => new Response('not JSON'));
  assert.throws(() => validateSession(data));
});

test('Les composants React affichent la connexion et la gestion des comptes selon le role', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  t.after(() => server.close());
  const { AuthContext, LoginForm } = await server.ssrLoadModule('/src/Auth.jsx');
  const { default: Accounts } = await server.ssrLoadModule('/src/Accounts.jsx');
  const login = renderToStaticMarkup(React.createElement(LoginForm, { onLogin() {} }));
  assert.match(login, /Connexion au pilotage/);
  assert.match(login, /type="password"/);
  assert.match(login, /autocomplete="current-password"/i);
  assert.doesNotMatch(login, /value="[^"]+"/);
  function accountMarkup(role) {
    return renderToStaticMarkup(React.createElement(AuthContext.Provider,
      { value: { enabled: true, user: { ...user, role }, csrfToken: csrf } }, React.createElement(Accounts)));
  }
  const reader = accountMarkup('reader');
  assert.match(reader, /Mon compte/);
  assert.match(reader, /Changer mon mot de passe/);
  assert.doesNotMatch(reader, /Créer un compte|Comptes et accès|Nom affiché/);
  const admin = accountMarkup('admin');
  assert.match(admin, /Comptes et accès/);
  assert.match(admin, /Créer un compte/);
  assert.match(admin, /Administrateur/);
});
