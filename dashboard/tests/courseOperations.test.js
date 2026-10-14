import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { operationActive, operationKey, validateCourseCatalog, validateCourseMutation,
  validateCourseOperations } from '../src/courseOperations.js';

const at = '2026-10-14T18:00:00Z';
const id = 'a'.repeat(32);
const base = (kind = 'backup', status = 'queued') => ({ id, client_key: 'b'.repeat(32), kind, status,
  requested_by: { id: 1, username: 'admin' }, created_at: at, updated_at: at,
  started_at: status === 'queued' ? null : at, finished_at: ['queued', 'running'].includes(status) ? null : at,
  course_id: 2, course: { id: 2, name: 'Cours PFE', shortname: 'PFE', category_id: 1 },
  backup_id: kind === 'restore' ? 'c'.repeat(32) : null, category_id: kind === 'restore' ? 1 : null,
  backup: null, restored_course: null, reason_code: null, message: null });
const envelope = (items) => ({ status: 'available', mode: 'course_only', evaluated_at: at,
  items, total: items.length, limit: 30, offset: 0, has_more: false });

test('le catalogue et les opérations de cours sont validés strictement', () => {
  const catalog = validateCourseCatalog({ status: 'available', mode: 'course_only', evaluated_at: at,
    generated_at: at, courses: [{ id: 2, name: 'Cours PFE', shortname: 'PFE', category_id: 1 }],
    categories: [{ id: 1, name: 'Divers' }] });
  assert.equal(catalog.courses[0].id, 2);
  const backup = { ...base('backup', 'completed'), backup_id: id, backup: { id, filename: 'pfe.mbz',
    size_bytes: 2048, sha256: 'd'.repeat(64), download_url: `/api/v1/course-operations/${id}/download` } };
  assert.equal(validateCourseOperations(envelope([backup])).items[0].backup.filename, 'pfe.mbz');
  const restore = { ...base('restore', 'completed'), restored_course: { id: 9, name: 'Cours PFE 2', shortname: 'PFE_2', category_id: 1 } };
  assert.equal(validateCourseMutation({ status: 'available', mode: 'course_only', evaluated_at: at,
    created: true, operation: restore }).operation.restored_course.id, 9);
  assert.equal(operationActive(base()), true);
  assert.equal(operationActive(backup), false);
});

test('les résultats incohérents et les liens arbitraires sont refusés', () => {
  const backup = { ...base('backup', 'completed'), backup_id: id, backup: { id, filename: 'pfe.mbz',
    size_bytes: 2048, sha256: 'd'.repeat(64), download_url: '/api/v1/course-operations/delete' } };
  assert.throws(() => validateCourseOperations(envelope([backup])));
  assert.throws(() => validateCourseCatalog({ status: 'available', mode: 'course_only', generated_at: at,
    courses: [{ id: 0, name: 'Site' }], categories: [] }));
  assert.throws(() => validateCourseMutation({ status: 'available', mode: 'course_only', created: 'oui', operation: base() }));
});

test('la clé idempotente est générée localement sur 128 bits', () => {
  assert.equal(operationKey({ getRandomValues: (bytes) => bytes.fill(15) }), '0f'.repeat(16));
});

test('Nginx expose seulement les routes de cours prévues', async () => {
  const source = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  assert.match(source, /location = \/api\/v1\/course-operations \{\s+limit_except GET/);
  assert.match(source, /course-operations\/\(backups\|restores\).*limit_except POST/s);
  assert.match(source, /course-operations\/\[0-9a-f\].*\/download.*limit_except GET/s);
});
