import { SERVICE_KEYS } from '../src/applicationMetrics.js';

export function dashboardFixtures(now = Date.parse('2026-10-11T21:30:00Z')) {
  const iso = (delta = 0) => new Date(now + delta).toISOString().replace('.000Z', 'Z');
  const at = iso();
  const freeze = iso(-12 * 3600_000);
  const snapshot = { source: 'prometheus', evaluated_at: at, metrics: { cpu_usage_percent: 92, memory_usage_percent: 52,
    disk_available_percent: 67, moodle_available: true, moodle_probe_duration_ms: 125, moodle_http_status_code: 200 },
    unavailable_metrics: [], collection: { linux: { status: 'up', scrape_age_seconds: 2 }, moodle_https: { status: 'up', scrape_age_seconds: 2 } } };
  const summary = { status: 'available', evaluated_at: at, counts: { open: 2, resolved: 4 },
    collector: { status: 'running', age_seconds: 2, input_age_seconds: 2 } };
  const incidents = { items: [
    { id: 2, title: 'File d’attente PHP-FPM', signal_key: 'php_pool_queue', severity: 'warning', category: 'application', latest_state: 'triggered',
      status: 'open', opened_at: iso(-120000), first_observed_at: iso(-120000), last_observed_at: at, resolved_at: null },
    { id: 1, title: 'CPU élevé', signal_key: 'cpu_high', severity: 'critical', category: 'resource', latest_state: 'triggered',
      status: 'open', opened_at: iso(-180000), first_observed_at: iso(-180000), last_observed_at: at, resolved_at: null },
  ], total: 2, limit: 10, offset: 0, has_more: false };
  const applications = { status: 'available', source: 'prometheus', mode: 'read_only', evaluated_at: at, max_scrape_age_seconds: 60,
    services: Object.fromEntries(Object.entries(SERVICE_KEYS).map(([name, keys]) => [name, {
      status: 'available', collection: { status: 'up', scrape_age_seconds: 2 }, service_reachable: true,
      metrics: Object.fromEntries(keys.map((key) => [key, 0])), unavailable_metrics: [],
    }])) };
  applications.services.php_fpm.metrics.listen_queue = 2;
  const containers = { status: 'available', source: 'prometheus', mode: 'read_only', evaluated_at: at,
    max_scrape_age_seconds: 60, max_collection_age_seconds: 90,
    collection: { status: 'up', scrape_age_seconds: 2 }, collector: { status: 'up', age_seconds: 2 },
    containers: ['apache', 'php', 'postgres'].map((service) => ({ project: 'moodle-pilote', service,
      state: 'running', health: null, instances: 1, stats_age_seconds: 2, metrics_status: 'available',
      metrics: { cpu_usage_percent: 0, memory_working_set_bytes: 1048576, memory_limit_bytes: 2097152, pids: 2 } })) };
  const backup = { status: 'available', mode: 'read_only', evaluated_at: at, backup_state: 'recent', reason: null,
    latest_success: { snapshot: 'a'.repeat(64), frozen_at_utc: freeze, completed_at_utc: iso(-12 * 3600_000 + 300_000), age_hours: 12, duration_seconds: 300 },
    policy: { max_age_hours: 24 }, schedule: { state: 'active', enabled: true }, job: { state: 'idle', last_result: 'success' },
    collection: { status: 'up', observed_at: at, age_seconds: 0, max_age_seconds: 150 } };
  const pilotage = { ...structuredClone(backup), scope: 'pilotage_postgresql', restoration_state: 'validated', restore_collection_status: 'recorded', same_snapshot: true,
    latest_success: { ...backup.latest_success, tables: 31, rows: 415, repository_check: 'passed' },
    latest_restore: { status: 'restore_validated', run_id: 'c'.repeat(32), snapshot: 'a'.repeat(64), captured_at: freeze,
      started_at: iso(-11 * 3600_000), completed_at: iso(-11 * 3600_000 + 77000), duration_seconds: 77,
      tables_verified: 31, rows_verified: 415, sequence_check: 'passed', cleanup: { container_removed: true, temporary_files_removed: true } } };
  const catalog = { status: 'available', mode: 'read_only', scope: 'moodle', evaluated_at: at, reason: null,
    total: 2, listed_total: 2, truncated: false, limit: 10, offset: 0, has_more: false,
    latest_success_id: 'a'.repeat(64), latest_success_presence: 'present',
    collection: { status: 'up', observed_at: at, age_seconds: 0, max_age_seconds: 900 }, verification: { repository_integrity: 'not_checked', restoration: 'not_checked' },
    items: [{ id: 'a'.repeat(64), archived_at: backup.latest_success.completed_at_utc, recovery_at: freeze, hostname: 'moodleprod', success_recorded: true },
      { id: 'b'.repeat(64), archived_at: iso(-24 * 3600_000), recovery_at: null, hostname: 'moodleprod', success_recorded: false }] };
  const decision = (id) => ({ evaluated_at: at, snapshot_usable: true, decision: { incident_id: id, status: 'recommendation',
    signal_key: id === 1 ? 'cpu_high' : 'php_pool_queue', context: 'normal', latest_state: 'triggered', priority: 'warning',
    explanation: 'Signal confirmé avec des mesures récentes. Un diagnostic est recommandé.', input_evaluated_at: at, valid_until: iso(90000),
    policy_version: '1.1.0', policy_sha256: 'b'.repeat(64), decision_id: 'c'.repeat(64), execution_allowed: false,
    reason_codes: ['confirmed_recent_signal'], evidence: { cpu: 92 },
    recommended_action: { title: id === 1 ? 'Diagnostic CPU' : 'Diagnostic du pool PHP-FPM', steps: ['Examiner les mesures et les dépendances'], executable: false } } });
  const history = { execution_allowed: false, items: [{ id: 2, incident_id: 1, status: 'recommendation', persisted: true,
    first_observed_at: at, last_observed_at: at, observations: 1, decision: decision(1).decision,
    policy: { version: '1.1.0', sha256: 'b'.repeat(64) }, collector: { status: 'running' } }], has_more: false, next_before_id: null,
    coverage: { status: 'running', started_at: at, last_input_at: at, last_evaluated_at: at, backfilled: false } };
  const recovery = { status: 'available', mode: 'approval_only', execution_enabled: false, evaluated_at: at,
    items: [{ id: 'd'.repeat(32), scope: 'moodle', snapshot_id: 'a'.repeat(64), target: 'isolated_test', reason: 'Vérifier cette copie dans un environnement isolé',
      status: 'pending', revision: 1, requested_by: { id: 1, username: 'hamma' }, created_at: at, updated_at: at,
      selection: { scope: 'moodle', catalog_observed_at: at, point: catalog.items[0] }, decision: null, execution: { enabled: false, status: 'not_dispatched' } }],
    total: 1, limit: 10, offset: 0, has_more: false };
  return { at, snapshot, summary, incidents, applications, containers, backup, pilotage, catalog, decision, history, recovery };
}
