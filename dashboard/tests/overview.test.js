import assert from 'node:assert/strict';
import test from 'node:test';
import { buildView, validateSnapshot } from '../src/snapshot.js';
import { validateDocument } from '../src/operations.js';
import { OVERVIEW_REQUESTS, currentRecommendations, importantIncidents, moodleBackupStatus, moodleStatus, observerCurrent } from '../src/navigation.js';
import { dashboardFixtures } from './fixtures.js';

const now = Date.parse('2026-10-11T21:30:00Z');
function inputs() {
  const fixture = dashboardFixtures(now);
  const readings = Object.fromEntries(OVERVIEW_REQUESTS.map(({ key, kind }) => [key, { data: validateDocument(kind, fixture[key]), pending: false }]));
  const decisions = Object.fromEntries([1, 2].map((id) => [id, { data: validateDocument('decision', fixture.decision(id)) }]));
  return { fixture, readings, decisions, view: buildView(validateSnapshot(fixture.snapshot), now) };
}

test('the overview reuses only existing reads and does not request pilotage backups', () => {
  const paths = OVERVIEW_REQUESTS.map(({ path }) => path);
  assert.ok(paths.includes('/api/v1/backups/summary'));
  assert.ok(paths.includes('/api/v1/backups/catalog?scope=moodle&limit=10&offset=0'));
  assert.ok(paths.includes('/api/v1/incidents?limit=10&offset=0&status=open'));
  assert.ok(paths.every((path) => !/pilotage|execute|restore|policies/.test(path)));
});

test('Moodle unavailability is distinct from an interrupted or old HTTPS collection', () => {
  const { fixture, readings, view } = inputs();
  assert.equal(moodleStatus(view, readings, now), 'operational');
  fixture.snapshot.metrics.moodle_available = false;
  assert.equal(moodleStatus(buildView(fixture.snapshot, now), readings, now), 'unavailable');
  fixture.snapshot.collection.moodle_https.status = 'down';
  assert.equal(moodleStatus(buildView(fixture.snapshot, now), readings, now), 'unknown');
  assert.equal(moodleStatus(buildView(fixture.snapshot, now + 61000), readings, now + 61000), 'stale');
  assert.equal(moodleStatus(buildView(fixture.snapshot, now, 'Erreur réseau'), readings, now), 'unknown');
});

test('a current dependency failure degrades an accessible Moodle without inventing a probe failure', () => {
  const { readings, view } = inputs();
  readings.applications.data.services.php_fpm.service_reachable = false;
  assert.equal(moodleStatus(view, readings, now), 'degraded');
  readings.applications.data.services.php_fpm.collection.status = 'down';
  assert.equal(moodleStatus(view, readings, now), 'operational');
  readings.applications.data.services.postgresql.service_reachable = false;
  assert.equal(moodleStatus(view, readings, now), 'degraded');
});

test('a recent backup needs a matching point present in a current repository catalogue', () => {
  const { readings } = inputs();
  assert.equal(moodleBackupStatus(readings, now).status, 'operational');
  assert.equal(moodleBackupStatus(readings, now).ageHours, 12);
  readings.catalog.data.latest_success_id = 'e'.repeat(64);
  assert.equal(moodleBackupStatus(readings, now).presence, 'unknown');
  assert.equal(moodleBackupStatus(readings, now).status, 'unknown');
  readings.catalog.data.latest_success_id = readings.backup.data.latest_success.snapshot;
  readings.catalog.data.latest_success_presence = 'absent';
  assert.equal(moodleBackupStatus(readings, now).status, 'warning');
  readings.catalog.data.latest_success_presence = 'present';
  readings.backup.data.job.last_result = 'failed';
  assert.equal(moodleBackupStatus(readings, now).status, 'warning');
  assert.equal(moodleBackupStatus(readings, now + 61000).status, 'stale');
});

test('missing backups and an unreadable catalogue do not become confirmed availability', () => {
  const { readings } = inputs();
  readings.backup.data.latest_success = null;
  readings.backup.data.backup_state = 'missing';
  assert.equal(moodleBackupStatus(readings, now).status, 'warning');
  assert.equal(moodleBackupStatus(readings, now).presence, 'unknown');
  readings.catalog = { data: null, error: 'Dépôt inaccessible' };
  readings.backup = { data: null, error: 'Lecture indisponible' };
  assert.equal(moodleBackupStatus(readings, now).status, 'unknown');
});

test('recommendations require a current observer, matching incident and unexpired decision', () => {
  const { readings, decisions } = inputs();
  const incidents = importantIncidents(readings.incidents);
  assert.deepEqual(incidents.map((item) => item.id), [1, 2]);
  assert.equal(currentRecommendations(incidents, decisions, readings.summary.data, now).length, 2);
  for (const moment of [now - 31000, now + 91000]) {
    assert.equal(currentRecommendations(incidents, decisions, readings.summary.data, moment).length, 0);
  }
  decisions[1].data.decision.valid_until = new Date(now).toISOString();
  assert.equal(currentRecommendations(incidents, decisions, readings.summary.data, now).length, 1);
  decisions[2].data.snapshot_usable = false;
  assert.equal(currentRecommendations(incidents, decisions, readings.summary.data, now).length, 0);
  assert.equal(observerCurrent({ ...readings.summary.data, collector: { status: 'running', age_seconds: -1, input_age_seconds: 0 } }, now), false);
});

test('the operational homepage hides PostgreSQL, isolated restore summaries and execution controls', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { OverviewView } = await server.ssrLoadModule('/src/ApplicationServices.jsx');
  const props = inputs();
  const render = (value) => renderToStaticMarkup(React.createElement(OverviewView, { ...value, now }));
  const html = render(props);
  for (const label of ['Moodle est accessible', 'Alertes et incidents', 'Sauvegardes Moodle', 'Ressources principales', 'Services utiles', 'Actions recommandées', 'Diagnostic CPU']) assert.match(html, new RegExp(label));
  assert.doesNotMatch(html, /PostgreSQL|Dernier test de restauration isolée|Test isolé|Lancer le test|>Exécuter|>Approuver|<pre/);
  assert.match(html, /12 h|12,0 h/);
  const unknown = render({ ...props, readings: {}, decisions: {}, view: buildView(null, now) });
  assert.match(unknown, /Disponibilité à confirmer/);
  assert.doesNotMatch(unknown, /Aucun incident ouvert enregistré|Aucune action recommandée pour les incidents ouverts|Diagnostic CPU|Opérationnel/);
});
