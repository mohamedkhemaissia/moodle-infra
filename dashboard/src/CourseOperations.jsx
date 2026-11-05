import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { canManageUsers, jsonRequest } from './authClient.js';
import { operationActive, operationKey, validateCourseCatalog, validateCourseMutation,
  validateCourseOperations } from './courseOperations.js';

const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const sizes = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
const labels = { queued: 'En attente', running: 'En cours', completed: 'Terminée', failed: 'Échec', interrupted: 'Interrompue' };

function size(bytes) { return Number.isFinite(bytes) ? `${sizes.format(bytes / 1024 / 1024)} Mo` : '—'; }

export default function CourseOperations({ refreshToken }) {
  const auth = useAuth();
  const [catalog, setCatalog] = useState(null);
  const [operations, setOperations] = useState([]);
  const [courseId, setCourseId] = useState('');
  const [backupId, setBackupId] = useState('');
  const [categoryId, setCategoryId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async (signal) => {
    try {
      const [nextCatalog, history] = await Promise.all([
        jsonRequest('/api/v1/course-operations/catalog', { signal }),
        jsonRequest('/api/v1/course-operations?limit=30&offset=0', { signal }),
      ]);
      const checkedCatalog = validateCourseCatalog(nextCatalog);
      const checkedHistory = validateCourseOperations(history);
      setCatalog(checkedCatalog); setOperations(checkedHistory.items); setError('');
      setCourseId((before) => before || String(checkedCatalog.courses[0]?.id ?? ''));
      setCategoryId((before) => before || String(checkedCatalog.categories[0]?.id ?? ''));
      const available = checkedHistory.items.find((item) => item.kind === 'backup' && item.status === 'completed');
      setBackupId((before) => checkedHistory.items.some((item) => item.id === before && item.status === 'completed')
        ? before : (available?.id ?? ''));
    } catch (reason) {
      if (reason?.name !== 'AbortError') setError(reason?.message || 'Sauvegardes de cours indisponibles.');
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load, refreshToken]);

  const active = operations.some(operationActive);
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => load(), 2000);
    return () => clearInterval(timer);
  }, [active, load]);

  const backups = useMemo(() => operations.filter((item) => item.kind === 'backup' && item.status === 'completed'), [operations]);
  const allowed = canManageUsers(auth.user);

  async function submit(path, body, success) {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = validateCourseMutation(await jsonRequest(path, { method: 'POST', csrfToken: auth.csrfToken, body }));
      setNotice(success); setOperations((before) => [result.operation, ...before.filter((row) => row.id !== result.operation.id)]);
      await load();
    } catch (reason) {
      setError(reason?.message || 'Demande refusée.');
    } finally { setBusy(false); }
  }

  return <div className="course-operations">
    <div className="backup-area-heading"><div><h3>Sauvegarde et restauration de cours</h3>
      <p className="ops-caption">Archive Moodle <code>.mbz</code> avec restauration uniquement comme nouveau cours.</p></div>
      <span className="ops-badge good">Cours uniquement</span></div>
    {error && <p className="notice" role="alert">{error}</p>}
    {notice && <p className="course-operation-success" role="status">{notice}</p>}
    {!catalog ? !error && <p className="ops-empty">Lecture du catalogue Moodle…</p> : <>
      <div className="course-operation-actions">
        <form onSubmit={(event) => { event.preventDefault(); submit('/api/v1/course-operations/backups',
          { course_id: Number(courseId), client_key: operationKey() }, 'Sauvegarde ajoutée à la file.'); }}>
          <h4>1. Sauvegarder un cours</h4>
          <label>Cours<select value={courseId} onChange={(event) => setCourseId(event.target.value)} disabled={busy || !allowed}>
            {catalog.courses.map((course) => <option key={course.id} value={course.id}>{course.name} ({course.shortname})</option>)}
          </select></label>
          <button className="primary-button" type="submit" disabled={busy || !allowed || !courseId}>Créer le fichier .mbz</button>
        </form>
        <form onSubmit={(event) => { event.preventDefault(); submit('/api/v1/course-operations/restores',
          { backup_id: backupId, category_id: Number(categoryId), client_key: operationKey() },
          'Restauration ajoutée à la file. Le cours actif ne sera pas remplacé.'); }}>
          <h4>2. Restaurer une copie</h4>
          <label>Sauvegarde<select value={backupId} onChange={(event) => setBackupId(event.target.value)} disabled={busy || !allowed}>
            <option value="">Choisir une sauvegarde</option>
            {backups.map((operation) => <option key={operation.id} value={operation.id}>{operation.course?.name || `Cours #${operation.course_id}`} · {dates.format(Date.parse(operation.finished_at))}</option>)}
          </select></label>
          <label>Catégorie<select value={categoryId} onChange={(event) => setCategoryId(event.target.value)} disabled={busy || !allowed}>
            {catalog.categories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
          </select></label>
          <button className="primary-button" type="submit" disabled={busy || !allowed || !backupId || !categoryId}>Restaurer comme nouveau cours</button>
        </form>
      </div>
      {!allowed && <p className="ops-section-note">Un compte administrateur est requis pour lancer une opération.</p>}
      <h4>Historique récent</h4>
      {operations.length === 0 ? <p className="ops-empty">Aucune sauvegarde de cours enregistrée.</p> :
        <div className="course-operation-history">{operations.map((operation) => <article key={operation.id}>
          <div><strong>{operation.kind === 'backup' ? 'Sauvegarde' : 'Restauration'} · {operation.course?.name || `Cours #${operation.course_id}`}</strong>
            <small>{dates.format(Date.parse(operation.created_at))} · demande de {operation.requested_by.username}</small></div>
          <span className={`ops-badge ${operation.status === 'completed' ? 'good' : ['failed', 'interrupted'].includes(operation.status) ? 'bad' : 'neutral'}`}>{labels[operation.status]}</span>
          {operation.status === 'completed' && operation.kind === 'backup' && <div className="course-operation-result">
            <span>{size(operation.backup.size_bytes)} · SHA-256 vérifié</span>
            <a className="secondary-button" href={operation.backup.download_url}>Télécharger .mbz</a></div>}
          {operation.status === 'completed' && operation.kind === 'restore' && <p className="course-operation-result">
            Nouveau cours créé : <strong>{operation.restored_course.name}</strong> (ID {operation.restored_course.id}).</p>}
          {['failed', 'interrupted'].includes(operation.status) && <p className="ops-empty">{operation.message || 'Opération non terminée.'}</p>}
        </article>)}</div>}
      <p className="ops-section-note">Cette démonstration protège un cours Moodle. La sauvegarde complète du site et le PRA hors site restent une perspective de déploiement sur la VM.</p>
    </>}
  </div>;
}
