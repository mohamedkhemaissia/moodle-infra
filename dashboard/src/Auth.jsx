import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { jsonRequest, validateSession } from './authClient.js';

export const AuthContext = createContext(null);
const Context = AuthContext;
export function useAuth() { return useContext(Context); }

export function LoginForm({ onLogin, message }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError(null);
    try { await onLogin(username, password); setPassword(''); }
    catch (cause) { setError(cause.message); setPassword(''); }
    finally { setBusy(false); }
  }
  return <main className="auth-screen"><section className="card auth-card" aria-labelledby="login-title">
    <div className="auth-brand">Moodle <span>BeeCoders</span></div>
    <h1 id="login-title">Connexion au pilotage</h1><p>Accédez au tableau de bord de votre plateforme.</p>
    {(error || message) && <p className="auth-error" role="alert">{error || message}</p>}
    <form onSubmit={submit} className="auth-form">
      <label htmlFor="login-user">Identifiant</label>
      <input id="login-user" autoComplete="username" value={username} maxLength={32}
        required onChange={(event) => setUsername(event.target.value)} disabled={busy} />
      <label htmlFor="login-password">Mot de passe</label>
      <input id="login-password" type="password" autoComplete="current-password" maxLength={128}
        required value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} />
      <button className="auth-primary" disabled={busy} type="submit">{busy ? 'Connexion…' : 'Se connecter'}</button>
    </form>
  </section></main>;
}

export default function AuthGate({ children }) {
  const [phase, setPhase] = useState('loading');
  const [session, setSession] = useState(null);
  const [message, setMessage] = useState(null);
  const sessionRef = useRef(null);
  const accept = useCallback((value) => {
    const next = validateSession(value);
    sessionRef.current = next; setSession(next); setPhase('ready'); setMessage(null);
  }, []);
  const check = useCallback(async (signal) => {
    setPhase('loading'); setMessage(null);
    try { accept(await jsonRequest('/api/v1/auth/session', { signal })); }
    catch (error) {
      if (error.name === 'AbortError') return;
      if (error.status === 401) { sessionRef.current = null; setSession(null); setPhase('login'); }
      else { setMessage('Le service de connexion est indisponible. Réessayez.'); setPhase('error'); }
    }
  }, [accept]);
  useEffect(() => {
    const controller = new AbortController();
    const expired = () => {
      if (!sessionRef.current?.user) return;
      sessionRef.current = null; setSession(null); setPhase('login');
      setMessage('Votre session a expiré. Reconnectez-vous.');
    };
    window.addEventListener('pilotage-session-expired', expired);
    check(controller.signal);
    return () => { controller.abort(); window.removeEventListener('pilotage-session-expired', expired); };
  }, [check]);
  async function login(username, password) {
    accept(await jsonRequest('/api/v1/auth/login', { method: 'POST', body: { username, password } }));
  }
  async function logout() {
    try {
      await jsonRequest('/api/v1/auth/logout', { method: 'POST', csrfToken: session.csrfToken });
      sessionRef.current = null; setSession(null); setPhase('login'); setMessage(null);
    } catch { setMessage('Déconnexion impossible pour le moment. Réessayez.'); }
  }
  if (phase === 'loading') return <main className="auth-screen"><p role="status">Ouverture du pilotage…</p></main>;
  if (phase === 'error') return <main className="auth-screen"><section className="card auth-card">
    <h1>Connexion indisponible</h1><p role="alert">{message}</p>
    <button className="auth-primary" onClick={() => check()}>Réessayer</button>
  </section></main>;
  if (phase === 'login') return <LoginForm onLogin={login} message={message} />;
  const user = session?.user ?? { id: 0, display_name: 'Consultation', role: 'reader', active: true };
  return <Context.Provider value={{ user, enabled: session.enabled, csrfToken: session.csrfToken,
    logout, accept, message }}>{children}</Context.Provider>;
}
