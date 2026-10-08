import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { canManageUsers, jsonRequest, validateUser } from './authClient.js';

function PasswordForm() {
  const auth = useAuth();
  const [oldPassword, setOld] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault(); setNotice(null);
    if (password !== confirmation) { setNotice('Les deux nouveaux mots de passe sont différents.'); return; }
    setBusy(true);
    try {
      const result = await jsonRequest('/api/v1/auth/password', { method: 'POST', csrfToken: auth.csrfToken,
        body: { old_password: oldPassword, new_password: password } });
      auth.accept(result); setNotice('Mot de passe modifié. Les anciennes sessions ont été fermées.');
    } catch (error) { setNotice(error.message); }
    finally { setOld(''); setPassword(''); setConfirmation(''); setBusy(false); }
  }
  return <details className="account-details"><summary>Changer mon mot de passe</summary>
    <form className="auth-form account-form" onSubmit={submit}>
      <label>Mot de passe actuel<input type="password" autoComplete="current-password" required maxLength={128}
        value={oldPassword} onChange={(event) => setOld(event.target.value)} disabled={busy} /></label>
      <label>Nouveau mot de passe<input type="password" autoComplete="new-password" required minLength={15} maxLength={128}
        value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} /></label>
      <label>Confirmer le nouveau mot de passe<input type="password" autoComplete="new-password" required minLength={15}
        maxLength={128} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy} /></label>
      <small>15 caractères minimum.</small>
      {notice && <p role="status">{notice}</p>}
      <button className="auth-primary" disabled={busy}>Enregistrer le mot de passe</button>
    </form>
  </details>;
}

function UserManager() {
  const auth = useAuth();
  const [users, setUsers] = useState([]);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ username: '', display_name: '', role: 'reader', password: '' });
  async function load(signal) {
    const data = await jsonRequest('/api/v1/users', { signal });
    if (!Array.isArray(data?.items)) throw new Error('Liste des comptes invalide.');
    setUsers(data.items.map(validateUser));
  }
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).catch((cause) => { if (cause.name !== 'AbortError') setError(cause.message); });
    return () => controller.abort();
  }, []);
  async function add(event) {
    event.preventDefault(); setBusy(true); setError(null); setNotice(null);
    try {
      await jsonRequest('/api/v1/users', { method: 'POST', csrfToken: auth.csrfToken, body: form });
      setForm({ username: '', display_name: '', role: 'reader', password: '' });
      await load(); setNotice('Compte créé.');
    } catch (cause) { setError(cause.message); setForm((value) => ({ ...value, password: '' })); }
    finally { setBusy(false); }
  }
  async function update(user, body) {
    setBusy(true); setError(null); setNotice(null);
    try {
      await jsonRequest('/api/v1/users/' + user.id, { method: 'PATCH', csrfToken: auth.csrfToken, body });
      if (user.id === auth.user.id) {
        await jsonRequest('/api/v1/auth/session');
      } else { await load(); setNotice('Compte mis à jour. Ses anciennes sessions sont fermées.'); }
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  }
  function field(name, value) { setForm((current) => ({ ...current, [name]: value })); }
  return <>
    {(error || notice) && <p className={error ? 'auth-error' : 'account-notice'} role={error ? 'alert' : 'status'}>{error || notice}</p>}
    <div className="account-table-wrap"><table className="account-table"><thead><tr>
      <th>Compte</th><th>Identifiant</th><th>Rôle</th><th>État</th><th>Action</th>
    </tr></thead><tbody>{users.map((user) => <tr key={user.id}>
      <td>{user.display_name}</td><td>{user.username}</td><td>
        <select aria-label={'Rôle de ' + user.username} value={user.role} disabled={busy}
          onChange={(event) => update(user, { role: event.target.value })}>
          <option value="reader">Lecteur</option><option value="admin">Administrateur</option>
        </select></td><td>{user.active ? 'Actif' : 'Désactivé'}</td>
      <td><button className="refresh-button" disabled={busy}
        onClick={() => update(user, { active: !user.active })}>{user.active ? 'Désactiver' : 'Réactiver'}</button></td>
    </tr>)}</tbody></table></div>
    <details className="account-details"><summary>Créer un compte</summary>
      <form className="auth-form account-form" onSubmit={add}>
        <label>Nom affiché<input required maxLength={80} value={form.display_name} disabled={busy}
          onChange={(event) => field('display_name', event.target.value)} /></label>
        <label>Identifiant<input required minLength={3} maxLength={32} pattern="[a-z][a-z0-9_.-]{2,31}"
          autoComplete="off" value={form.username} disabled={busy}
          onChange={(event) => field('username', event.target.value)} /></label>
        <label>Rôle<select value={form.role} disabled={busy} onChange={(event) => field('role', event.target.value)}>
          <option value="reader">Lecteur</option><option value="admin">Administrateur</option></select></label>
        <label>Mot de passe initial<input type="password" required minLength={15} maxLength={128}
          autoComplete="new-password" value={form.password} disabled={busy}
          onChange={(event) => field('password', event.target.value)} /></label>
        <small>15 caractères minimum.</small><button className="auth-primary" disabled={busy}>Créer le compte</button>
      </form>
    </details>
  </>;
}

export default function Accounts() {
  const auth = useAuth();
  if (!auth.enabled) return null;
  const admin = canManageUsers(auth.user);
  return <section id="accounts" className="card panel" aria-labelledby="accounts-heading">
    <div className="panel-heading"><div><h2 id="accounts-heading">{admin ? 'Comptes et accès' : 'Mon compte'}</h2>
      <p>{admin ? 'Gérez les comptes autorisés à accéder au pilotage.' : auth.user.display_name}</p>
    </div></div><div className="ops-body">
      {admin && <UserManager />}<PasswordForm />
    </div>
  </section>;
}
