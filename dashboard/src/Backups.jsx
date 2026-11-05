import SiteOperations from './SiteOperations.jsx';
import CourseOperations from './CourseOperations.jsx';
import { DetailsPanel } from './ApplicationServices.jsx';
import './backups.css';

export default function Backups({ refreshToken }) {
  return <section id="backups" className="panel ops-panel" aria-labelledby="backups-heading">
    <div className="panel-heading"><div><h2 id="backups-heading">Sauvegarde du site</h2>
      <p>Créer une sauvegarde complète ou restaurer un point précédent.</p></div></div>
    <div className="ops-body">
      <SiteOperations refreshToken={refreshToken} />
      <DetailsPanel title="Sauvegarde d’un cours" className="backup-secondary">
        <CourseOperations refreshToken={refreshToken} />
      </DetailsPanel>
    </div>
  </section>;
}
