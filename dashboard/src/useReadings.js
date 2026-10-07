import { useEffect, useState } from 'react';
import { freshResult, validateDocument } from './operations.js';

export function useReadings(requests, refreshToken, now) {
  const [results, setResults] = useState({});
  const signature = JSON.stringify(requests);
  useEffect(() => {
    const tasks = JSON.parse(signature);
    let active = true;
    let timer;
    let controllers = [];
    setResults({});
    async function poll() {
      const started = Date.now();
      controllers = [];
      await Promise.allSettled(tasks.map(async ({ key, path, kind }) => {
        const controller = new AbortController();
        controllers.push(controller);
        const timeout = setTimeout(() => controller.abort(), 8000);
        let result;
        try {
          const response = await fetch(path, { method: 'GET', signal: controller.signal,
            cache: 'no-store', headers: { Accept: 'application/json' } });
          if (!response.ok) throw new Error(`Lecture indisponible (HTTP ${response.status}).`);
          result = { data: validateDocument(kind, await response.json()), receivedAt: Date.now(), error: null };
        } catch (error) {
          result = { data: null, receivedAt: Date.now(), error: error.name === 'AbortError'
            ? 'Le délai de lecture est dépassé.' : error.message || 'Lecture indisponible.' };
        } finally { clearTimeout(timeout); }
        if (active) setResults((previous) => ({ ...previous, [key]: { ...result, path } }));
      }));
      if (active) timer = setTimeout(poll, Math.max(1000, 30_000 - (Date.now() - started)));
    }
    poll();
    return () => { active = false; clearTimeout(timer); controllers.forEach((c) => c.abort()); };
  }, [signature, refreshToken]);
  return Object.fromEntries(requests.map(({ key, path }) => {
    const result = results[key]?.path === path ? results[key] : null;
    return [key, { data: freshResult(result, now), error: result?.error,
      pending: !result, stale: !!result?.data && !freshResult(result, now) }];
  }));
}
