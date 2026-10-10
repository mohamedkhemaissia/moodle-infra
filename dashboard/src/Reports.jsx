import { useEffect, useState } from 'react';
import { PAGE_SIZE, REPORTS, loadReport, downloadReport } from './reports.js';
import './reports.css';

const date = (value) => new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC' }).format(Date.parse(value));

export function ReportView({ report, pending, error, onDownload, onPage, onRefresh }) {
  const preview = report?.kind === 'incidents' ? [0, 1, 5, 8] : report?.kind === 'recovery' ? [0, 1, 6, 9] : [0, 1, 3, 4];
  return <>
    <div className="reports-actions">
      <button className="refresh-button" disabled={pending} onClick={onRefresh}>Actualiser le journal</button>
      <button className="ops-button" disabled={!report || pending || !!error} onClick={() => onDownload('csv')}>Exporter la page CSV</button>
      <button className="ops-button" disabled={!report || pending || !!error} onClick={() => onDownload('html')}>Rapport HTML</button>
    </div>
    {pending ? <p className="ops-empty" role="status">Lecture du journal…</p> : error ?
      <p className="catalog-warning" role="alert">{error}</p> : report && <>
        <p className="ops-caption">Lecture du {date(report.retrieved_at)} UTC · {report.rows.length} entrée{report.rows.length > 1 ? 's' : ''} sur {report.total}.</p>
        <p className="reports-scope">Page {report.offset / PAGE_SIZE + 1} · {report.filter}. Les exports comprennent les données de cette page.
          {(report.offset > 0 || report.has_more) && ' D’autres pages existent et ne sont pas incluses.'}</p>
        {report.rows.length ? <div className="ops-table-wrap"><table className="reports-table"><thead><tr>
          {preview.map((position) => <th key={position}>{report.columns[position]}</th>)}
        </tr></thead><tbody>{report.rows.map((row) => <tr key={row[0]}>
          {preview.map((position) => <td key={position}>{row[position] === '' ? '—' : row[position]}</td>)}
        </tr>)}</tbody></table></div> : <p className="ops-empty">Aucune entrée sur cette page.</p>}
        <div className="ops-pagination"><span>{report.total} entrée{report.total > 1 ? 's' : ''} au moment de la lecture</span><div>
          <button disabled={pending || report.offset === 0} onClick={() => onPage(report.offset - PAGE_SIZE)}>Précédente</button>
          <button disabled={pending || !report.has_more || report.offset >= (report.kind === 'recovery' ? 10000 : 100000)}
            onClick={() => onPage(report.offset + PAGE_SIZE)}>Suivante</button>
        </div></div>
        <p className="ops-note">{report.note}</p>
      </>}
  </>;
}

export default function Reports({ refreshToken }) {
  const [kind, setKind] = useState('incidents');
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [reading, setReading] = useState({ pending: true, report: null, error: null });
  const [downloadError, setDownloadError] = useState(null);
  const key = `${kind}:${status}:${offset}:${localRefresh}:${refreshToken}`;
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    setReading({ pending: true, report: null, error: null }); setDownloadError(null);
    loadReport(kind, { status, offset, signal: controller.signal }).then((report) => {
      if (active) setReading({ pending: false, report, error: null, key });
    }).catch((error) => {
      if (active) setReading({ pending: false, report: null, error: error.name === 'AbortError' ? 'Le délai de lecture est dépassé.' : error.message, key });
    }).finally(() => clearTimeout(timeout));
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [kind, status, offset, localRefresh, refreshToken]);
  const current = reading.key === key ? reading : { pending: true, report: null, error: null };
  function download(format) {
    if (!current.report || current.pending || current.error) return;
    setDownloadError(null);
    try { downloadReport(current.report, format); }
    catch { setDownloadError('Le téléchargement n’a pas pu être préparé.'); }
  }
  return <section id="reports" className="panel ops-panel" aria-labelledby="reports-heading">
    <div className="panel-heading"><div><h2 id="reports-heading">Rapports</h2><p>Exporter les incidents et les opérations enregistrés.</p></div>
      <span className="subtle-badge">CSV · HTML</span></div>
    <div className="ops-body">
      <div className="reports-filters"><label>Journal <select value={kind} onChange={(event) => {
        setKind(event.target.value); setStatus(''); setOffset(0);
      }}>{Object.entries(REPORTS).map(([value, report]) => <option value={value} key={value}>{report.title}</option>)}</select></label>
        {kind === 'incidents' && <label>État <select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
          <option value="">Tous</option><option value="open">Ouverts</option><option value="resolved">Résolus</option></select></label>}
      </div>
      {downloadError && <p className="catalog-warning" role="alert">{downloadError}</p>}
      <ReportView {...current} onDownload={download} onPage={setOffset} onRefresh={() => setLocalRefresh((value) => value + 1)} />
    </div>
  </section>;
}
