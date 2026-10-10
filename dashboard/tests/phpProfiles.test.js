import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { validateProfiles } from '../src/phpProfiles.js';
import { recoveryProven } from '../src/operations.js';

const value = () => ({ status: 'available', mode: 'read_only', evaluated_at: '2026-10-10T16:00:00Z',
  recorded_at: '2026-10-10T15:50:00Z', state: 'prepared', recorded_profile: 'normal', activation_revision: 2,
  run_id: null, review_required: false, profiles: [{ id: 'normal', max_children: 5, sha256: 'a'.repeat(64) },
    { id: 'burst', max_children: 8, sha256: 'b'.repeat(64) }] });

test('un suivi historique conserve sa date et les deux limites fixes', () => {
  const d = validateProfiles(value()); assert.equal(d.recorded_at, '2026-10-10T15:50:00Z');
  assert.deepEqual(d.profiles.map((p) => p.max_children), [5, 8]);
  for (const modify of [(v) => { v.profiles[1].max_children = 50; },
    (v) => { v.profiles[1].id = 'normal'; }, (v) => { v.recorded_at = '2026-10-11T00:00:00Z'; }]) {
    const invalid = value(); modify(invalid); assert.throws(() => validateProfiles(invalid));
  }
});
test('le succes Runner seul et un retour arriere ne deviennent pas un changement confirme', () => {
  const d = { status: 'completed', action_id: 'apply_php_fpm_profile', report: {
    action_dispatched: true, recovered: true, reason: 'profile_reload_queue_and_https_confirmed' } };
  assert.equal(recoveryProven(d), true);
  assert.equal(recoveryProven({ ...d, status: 'failed' }), false);
  assert.equal(recoveryProven({ ...d, report: { ...d.report, recovered: false } }), false);
});
test('le proxy donne seulement une lecture du suivi des profils', async () => {
  const text = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const location = text.match(/location = \/api\/v1\/php-fpm\/profiles \{([\s\S]*?)\n        \}/)?.[1];
  assert.match(location, /limit_except GET \{ deny all; \}/);
  assert.match(location, /proxy_pass \$\{API_ORIGIN\}/);
});
