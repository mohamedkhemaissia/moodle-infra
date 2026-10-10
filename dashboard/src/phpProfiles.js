export function validateProfiles(value) {
  const date = (x) => typeof x === 'string' && Number.isFinite(Date.parse(x));
  const fail = () => { throw new Error('L’état des profils PHP est inexploitable.'); };
  if (!value || !['available', 'unavailable'].includes(value.status) || value.mode !== 'read_only' ||
      !date(value.evaluated_at) || !Array.isArray(value.profiles)) fail();
  if (value.status === 'unavailable') {
    if (value.profiles.length || typeof value.reason !== 'string' || value.recorded_profile !== null) fail();
    return value;
  }
  if (!date(value.recorded_at) || Date.parse(value.recorded_at) > Date.parse(value.evaluated_at) ||
      !['prepared', 'changing', 'verified', 'rolled_back', 'review_required'].includes(value.state) ||
      ![null, 'normal', 'burst'].includes(value.recorded_profile) || typeof value.review_required !== 'boolean' ||
      !Number.isSafeInteger(value.activation_revision) || value.activation_revision < 1 ||
      (value.run_id !== null && !/^[a-f0-9]{32}$/.test(value.run_id)) || value.profiles.length !== 2) fail();
  const seen = new Set();
  for (const profile of value.profiles) {
    if (!['normal', 'burst'].includes(profile.id) || seen.has(profile.id) ||
        profile.max_children !== { normal: 5, burst: 8 }[profile.id] || !/^[a-f0-9]{64}$/.test(profile.sha256)) fail();
    seen.add(profile.id);
  }
  return value;
}

export const PROFILE_NAMES = { normal: 'Normal', burst: 'Renforcé' };
export const PROFILE_STATES = { prepared: 'Profils préparés', changing: 'Changement en cours',
  verified: 'Changement vérifié', rolled_back: 'Retour au profil précédent', review_required: 'Intervention à examiner' };
