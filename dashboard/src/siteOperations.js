const idPattern = /^[0-9a-f]{32}$/;
const snapshotPattern = /^[0-9a-f]{64}$/;
const states = new Set(['queued', 'running', 'completed', 'failed', 'interrupted']);

export function validateSiteOperation(value) {
  if (!value || !idPattern.test(value.id ?? '') || !['backup', 'restore'].includes(value.kind)
      || !states.has(value.status) || !Number.isFinite(Date.parse(value.created_at))
      || !value.requested_by || !Number.isSafeInteger(value.requested_by.id)
      || typeof value.requested_by.username !== 'string') {
    throw new Error('Opération de sauvegarde invalide.');
  }
  return value;
}

export function validateSiteOperations(value) {
  if (value?.status !== 'available' || value.mode !== 'site_full' || !Array.isArray(value.points)
      || !Array.isArray(value.operations) || (value.latest_success_at !== null
        && !Number.isFinite(Date.parse(value.latest_success_at)))) {
    throw new Error('État de sauvegarde invalide.');
  }
  const points = value.points.map((point) => {
    if (!snapshotPattern.test(point?.id ?? '') || !Number.isFinite(Date.parse(point.created_at))) {
      throw new Error('Point de reprise invalide.');
    }
    return point;
  });
  return { ...value, points, operations: value.operations.map(validateSiteOperation) };
}

export function validateSiteMutation(value) {
  if (value?.status !== 'available' || value.mode !== 'site_full' || typeof value.created !== 'boolean') {
    throw new Error('Réponse de sauvegarde invalide.');
  }
  return { ...value, operation: validateSiteOperation(value.operation) };
}

export function siteOperationActive(value) { return ['queued', 'running'].includes(value?.status); }
