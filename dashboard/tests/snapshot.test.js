import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildView,
  chartSeries,
  sampleFromView,
  validateSnapshot,
} from '../src/snapshot.js';

const NOW = Date.parse('2026-10-06T14:20:20Z');
function snapshot() {
  return {
    evaluated_at: new Date(NOW).toISOString(),
    source: 'prometheus',
    collection: {
      linux: { status: 'up', scrape_age_seconds: 2 },
      moodle_https: { status: 'up', scrape_age_seconds: 8 },
    },
    metrics: {
      cpu_usage_percent: 0,
      memory_usage_percent: 26.393,
      disk_available_percent: 41.679,
      moodle_available: true,
      moodle_probe_duration_ms: 100.168,
      moodle_http_status_code: 200,
    },
    unavailable_metrics: [],
  };
}

test('Le CPU à zéro reste une mesure valide', () => {
  const view = buildView(validateSnapshot(snapshot()), NOW);
  assert.equal(view.metrics.cpu_usage_percent, 0);
  assert.equal(view.metrics.moodle_available, true);
});
test('Une sonde échouée est distincte d’une valeur absente', () => {
  const data = snapshot();
  Object.assign(data.metrics, {
    moodle_available: false,
    moodle_http_status_code: 0,
  });
  assert.equal(
    buildView(validateSnapshot(data), NOW).metrics.moodle_available,
    false,
  );
  data.metrics.moodle_available = null;
  assert.equal(
    buildView(validateSnapshot(data), NOW).metrics.moodle_available,
    null,
  );
});
test('Une erreur API empêche de réafficher Moodle comme disponible', () => {
  const view = buildView(snapshot(), NOW, 'Service inaccessible');
  assert.equal(view.reason, 'error');
  assert.equal(view.metrics.moodle_available, null);
  assert.equal(view.metrics.cpu_usage_percent, null);
});
test('Un instantané de plus de 60 secondes ne fournit aucune mesure courante', () => {
  const view = buildView(snapshot(), NOW + 61_000);
  assert.equal(view.reason, 'stale');
  assert.ok(Object.values(view.metrics).every((value) => value === null));
});
test('L’âge de collecte augmente après réception de l’instantané', () => {
  const data = snapshot();
  data.collection.linux.scrape_age_seconds = 55;
  const view = buildView(data, NOW + 10_000);
  assert.equal(view.collection.linux.status, 'stale');
  assert.equal(view.metrics.cpu_usage_percent, null);
  assert.equal(view.metrics.moodle_available, true);
});
test('Une collecte arrêtée supprime les valeurs de sa source seulement', () => {
  const data = snapshot();
  data.collection.moodle_https.status = 'down';
  const view = buildView(data, NOW);
  assert.equal(view.metrics.moodle_available, null);
  assert.equal(view.metrics.moodle_http_status_code, null);
  assert.equal(view.metrics.cpu_usage_percent, 0);
});
test('Une date trop avancée ne peut pas être considérée comme récente', () => {
  const data = snapshot();
  data.evaluated_at = new Date(NOW + 60_000).toISOString();
  const view = buildView(data, NOW);
  assert.equal(view.reason, 'clock');
  assert.equal(view.metrics.moodle_available, null);
});
test('Une collecte active sans âge est considérée manquante', () => {
  const data = snapshot();
  data.collection.linux.scrape_age_seconds = null;
  assert.equal(buildView(data, NOW).collection.linux.status, 'missing');
});
test('Les réponses incorrectes ou mal typées sont rejetées', () => {
  for (const mutate of [
    (data) => {
      data.evaluated_at = 'invalide';
    },
    (data) => {
      data.metrics.cpu_usage_percent = '32';
    },
    (data) => {
      data.metrics.moodle_available = 'false';
    },
    (data) => {
      delete data.metrics.memory_usage_percent;
    },
    (data) => {
      data.metrics.memory_usage_percent = Infinity;
    },
    (data) => {
      data.metrics.disk_available_percent = -1;
    },
    (data) => {
      data.metrics.moodle_http_status_code = 700;
    },
    (data) => {
      data.collection.linux.status = 'inconnu';
    },
  ]) {
    const data = snapshot();
    mutate(data);
    assert.throws(() => validateSnapshot(data));
  }
});
test('L’historique conserve les unités et les valeurs nulles', () => {
  const view = buildView(snapshot(), NOW);
  const sample = sampleFromView(view, NOW);
  assert.equal(sample.disk_available_percent, 41.679);
  assert.equal(sample.cpu_usage_percent, 0);
  assert.equal(
    sampleFromView(buildView(snapshot(), NOW, 'Panne'), NOW).cpu_usage_percent,
    null,
  );
});
test('Le tracé se coupe à chaque absence de mesure', () => {
  const series = chartSeries(
    [
      { time: NOW, value: 0 },
      { time: NOW + 15_000, value: null },
      { time: NOW + 30_000, value: 50 },
    ],
    'value',
  );
  assert.equal((series.path.match(/M/g) || []).length, 2);
  assert.ok(!series.path.includes('L'));
});
test('Le tracé ne relie pas deux mesures séparées par une longue interruption', () => {
  const series = chartSeries(
    [
      { time: NOW, value: 25 },
      { time: NOW + 120_000, value: 50 },
    ],
    'value',
  );
  assert.equal((series.path.match(/M/g) || []).length, 2);
  assert.equal(chartSeries([], 'value').path, '');
});
