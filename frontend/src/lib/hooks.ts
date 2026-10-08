"use client";

/**
 * Async data hooks — every page fetch goes through these so loading/error
 * state and effect cleanup are handled consistently (no leaked timers).
 *
 * State is keyed by a caller-supplied string (the URL key, an id, a filter):
 * a changed key renders the loading state immediately, and responses from a
 * superseded key are ignored.
 */

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "./api";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

interface RunRecord<T> {
  key: string;
  nonce: number;
  data: T | null;
  error: string | null;
  done: boolean;
}

function derive<T>(record: RunRecord<T>, key: string, nonce: number): AsyncState<T> {
  const sameKey = record.key === key;
  return {
    data: sameKey ? record.data : null,
    error: sameKey ? record.error : null,
    loading: !sameKey || record.nonce !== nonce || !record.done,
  };
}

/**
 * Run `load` whenever `key` (or `reload`) changes. `mutate` applies a fresh
 * server response — e.g. right after a PATCH — without refetching.
 */
export function useAsync<T>(
  load: () => Promise<T>,
  key: string,
): AsyncState<T> & { reload: () => void; mutate: (next: T) => void } {
  const [record, setRecord] = useState<RunRecord<T>>({
    key,
    nonce: 0,
    data: null,
    error: null,
    done: false,
  });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let active = true;
    load()
      .then((data) => {
        if (active) setRecord({ key, nonce, data, error: null, done: true });
      })
      .catch((error: unknown) => {
        if (active) {
          // Keep whatever data the same key already rendered; the error shows next to it.
          setRecord((previous) => ({
            key,
            nonce,
            data: previous.key === key ? previous.data : null,
            error: errorMessage(error),
            done: true,
          }));
        }
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const mutate = useCallback(
    (next: T) => setRecord({ key, nonce, data: next, error: null, done: true }),
    [key, nonce],
  );

  return { ...derive(record, key, nonce), reload, mutate };
}

/**
 * `useAsync` on a timer: re-fetches every `intervalMs` while `enabled`, and
 * always clears its timeout on cleanup (PRD §5.2 live progress, §35 refresh).
 * Pass `isDone` to stop polling as soon as a response qualifies (e.g. a job
 * reaching a terminal status).
 */
export function usePoll<T>(
  load: () => Promise<T>,
  intervalMs: number,
  enabled: boolean,
  key: string,
  isDone?: (data: T) => boolean,
): AsyncState<T> & { reload: () => void } {
  const [record, setRecord] = useState<RunRecord<T>>({
    key,
    nonce: 0,
    data: null,
    error: null,
    done: false,
  });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    let timer: number | undefined;
    let stopped = false;

    const tick = (): void => {
      load()
        .then((data) => {
          if (!active) return;
          setRecord({ key, nonce, data, error: null, done: true });
          if (isDone?.(data)) stopped = true;
        })
        .catch((error: unknown) => {
          if (active) {
            setRecord((previous) => ({
              key,
              nonce,
              data: previous.key === key ? previous.data : null,
              error: errorMessage(error),
              done: true,
            }));
          }
        })
        .finally(() => {
          if (active && !stopped) timer = window.setTimeout(tick, intervalMs);
        });
    };
    tick();

    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce, enabled, intervalMs]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { ...derive(record, key, nonce), reload };
}
