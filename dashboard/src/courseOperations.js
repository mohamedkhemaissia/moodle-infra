const idPattern = /^[0-9a-f]{32}$/;
const states = new Set(['queued', 'running', 'completed', 'failed', 'interrupted']);

function row(value, label) {
  if (!value || !Number.isSafeInteger(value.id) || value.id < 1 || typeof value.name !== 'string' || !value.name) {
    throw new Error(`${label} Moodle invalide.`);
  }
  return value;
}

export function validateCourseCatalog(value) {
  if (value?.status !== 'available' || value.mode !== 'course_only' || !Array.isArray(value.courses)
      || !Array.isArray(value.categories) || !Number.isFinite(Date.parse(value.generated_at))) {
    throw new Error('Catalogue Moodle invalide.');
  }
  return { ...value, courses: value.courses.map((item) => row(item, 'Cours')),
    categories: value.categories.map((item) => row(item, 'Catégorie')) };
}

export function validateCourseOperation(value) {
  if (!value || !idPattern.test(value.id ?? '') || !['backup', 'restore'].includes(value.kind)
      || !states.has(value.status) || !Number.isFinite(Date.parse(value.created_at))
      || !Number.isFinite(Date.parse(value.updated_at)) || !value.requested_by
      || !Number.isSafeInteger(value.requested_by.id) || value.requested_by.id < 1
      || typeof value.requested_by.username !== 'string') {
    throw new Error('Opération Moodle invalide.');
  }
  if (value.status === 'completed' && value.kind === 'backup') {
    const backup = value.backup;
    if (!backup || backup.id !== value.id || !idPattern.test(backup.id)
        || !Number.isSafeInteger(backup.size_bytes) || backup.size_bytes < 1
        || !/^[0-9a-f]{64}$/.test(backup.sha256 ?? '')
        || backup.download_url !== `/api/v1/course-operations/${value.id}/download`) {
      throw new Error('Résultat de sauvegarde invalide.');
    }
  }
  if (value.status === 'completed' && value.kind === 'restore') row(value.restored_course, 'Cours restauré');
  return value;
}

export function validateCourseOperations(value) {
  if (value?.status !== 'available' || value.mode !== 'course_only' || !Array.isArray(value.items)
      || !Number.isSafeInteger(value.total) || value.total < value.items.length || typeof value.has_more !== 'boolean') {
    throw new Error('Historique Moodle invalide.');
  }
  return { ...value, items: value.items.map(validateCourseOperation) };
}

export function validateCourseMutation(value) {
  if (value?.status !== 'available' || value.mode !== 'course_only' || typeof value.created !== 'boolean') {
    throw new Error('Réponse Moodle invalide.');
  }
  return { ...value, operation: validateCourseOperation(value.operation) };
}

export function operationKey(crypto = globalThis.crypto) {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
}

export function operationActive(operation) { return ['queued', 'running'].includes(operation?.status); }
