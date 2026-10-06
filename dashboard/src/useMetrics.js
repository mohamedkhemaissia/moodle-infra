import { useEffect, useState } from 'react';
import {
  buildView,
  REFRESH_MS,
  sampleFromView,
  validateSnapshot,
} from './snapshot.js';

export function useMetrics() {
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(true);
  const [samples, setSamples] = useState([]);
  const [refreshToken, setRefreshToken] = useState(0);
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let active = true;
    let nextPoll;
    let controller;
    let timeout;
    async function poll() {
      const started = Date.now();
      controller = new AbortController();
      timeout = setTimeout(() => controller.abort(), 8000);
      setBusy(true);
      try {
        const response = await fetch('/api/v1/metrics/summary', {
          signal: controller.signal,
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) {
          throw new Error(
            response.status === 503
              ? 'Prometheus ne répond pas.'
              : response.status === 502
                ? 'Les données de supervision sont inexploitables.'
                : `La lecture des mesures a échoué (HTTP ${response.status}).`,
          );
        }
        const data = validateSnapshot(await response.json());
        if (!active) return;
        const received = Date.now();
        setSnapshot(data);
        setError(null);
        setNow(received);
        const view = buildView(data, received);
        const sample = sampleFromView(view, received);
        setSamples((history) => [...history, sample].slice(-60));
      } catch (cause) {
        if (!active) return;
        const message =
          cause.name === 'AbortError'
            ? 'Le délai de lecture des mesures est dépassé.'
            : cause instanceof TypeError
              ? 'Impossible de joindre le service de supervision.'
              : cause instanceof SyntaxError
                ? 'Les données reçues ne sont pas exploitables.'
                : cause.message;
        setError(message);
        setNow(Date.now());
        setSamples((history) =>
          [
            ...history,
            {
              time: Date.now(),
              cpu_usage_percent: null,
              memory_usage_percent: null,
              disk_available_percent: null,
            },
          ].slice(-60),
        );
      } finally {
        clearTimeout(timeout);
        if (active) {
          setBusy(false);
          nextPoll = setTimeout(
            poll,
            Math.max(1000, REFRESH_MS - (Date.now() - started)),
          );
        }
      }
    }
    poll();
    return () => {
      active = false;
      clearTimeout(nextPoll);
      clearTimeout(timeout);
      controller?.abort();
    };
  }, [refreshToken]);

  return {
    snapshot,
    error,
    busy,
    samples,
    now,
    view: buildView(snapshot, now, error),
    refresh: () => setRefreshToken((token) => token + 1),
  };
}
