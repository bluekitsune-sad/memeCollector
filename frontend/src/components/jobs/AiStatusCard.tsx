"use client";

import { useEffect, useState } from "react";
import { errorMessage, getAiStatus } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { LoadingRow } from "@/components/StateBlocks";
import type { AiStatus, AiStatusState } from "@/lib/types";

/** Poll cadence for the live card (PRD §35). */
const POLL_MS = 3000;

/** Badge tone + copy per supervisor state (PRD §18). */
const STATE_PRESENTATION: Record<AiStatusState, { label: string; tone: string; hint: string }> = {
  processing: {
    label: "analyzing",
    tone: "processing",
    hint: "The background AI queue is analyzing collected media.",
  },
  on_hold: {
    label: "waiting to retry",
    tone: "processing",
    hint: "A temporary AI error deferred some items — they retry automatically.",
  },
  idle: {
    label: "idle",
    tone: "ready",
    hint: "No pending analysis: every claimable item is processed.",
  },
  unavailable: {
    label: "unavailable",
    tone: "failed",
    hint: "The AI provider cannot be started — fix the configuration to resume.",
  },
  stopped: {
    label: "stopped",
    tone: "failed",
    hint: "No background analysis loop is running.",
  },
};

/** Live background-AI status card: polls `/api/ai/status` every 3s (PRD §18, §35). */
export default function AiStatusCard() {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;

    async function load(): Promise<void> {
      // Background tab: skip the fetch, keep the existing timer chain.
      if (!alive || document.hidden) {
        schedule();
        return;
      }
      try {
        const next = await getAiStatus();
        if (!alive) return;
        setStatus(next);
        setError(null);
      } catch (caught) {
        if (alive) setError(errorMessage(caught));
      } finally {
        schedule();
      }
    }

    function schedule(): void {
      if (alive && timer === undefined) {
        timer = window.setTimeout(() => {
          timer = undefined;
          void load();
        }, POLL_MS);
      }
    }

    function onVisibilityChange(): void {
      if (document.visibilityState !== "visible" || !alive) return;
      // Tab visible again: fetch now instead of waiting out the last timer.
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
      void load();
    }

    void load();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  if (status === null && error === null) {
    return <LoadingRow label="Loading AI status…" />;
  }

  const presentation = status ? STATE_PRESENTATION[status.state] : null;

  return (
    <section className="panel" aria-live="polite">
      <div className="job-head">
        <span className="job-title">
          <i className={`status-icon ${status?.state === "processing" ? "running" : "completed"}`} aria-hidden="true" />
          AI analysis
        </span>
        {status && presentation ? (
          <span className={`badge ${presentation.tone}`}>{presentation.label}</span>
        ) : null}
      </div>

      {error ? <p className="inline-msg error">{error}</p> : null}
      {status && presentation ? <p className="job-message">{presentation.hint}</p> : null}

      {status?.state === "unavailable" && status.reason ? (
        <p className="job-error">{status.reason}</p>
      ) : null}

      {status?.state === "on_hold" ? (
        <p className="job-message">
          <b>
            {status.retry_in_seconds !== null ? `Retrying in ${status.retry_in_seconds}s` : "Retrying soon"}
          </b>
          {status.next_retry_at ? ` · next attempt ${formatDateTime(status.next_retry_at)}` : ""}
        </p>
      ) : null}

      {status?.job ? (
        <div className="counters">
          <div className="counter">
            <div className="label">Analyzed</div>
            <div className="value">
              {status.job.done} / {status.job.total}
            </div>
          </div>
          <div className="counter">
            <div className="label">Ready</div>
            <div className="value">{status.job.ready}</div>
          </div>
          <div className="counter">
            <div className="label">Retried later</div>
            <div className="value">{status.job.deferred}</div>
          </div>
          <div className="counter">
            <div className="label">Failed</div>
            <div className="value">{status.job.failed}</div>
          </div>
        </div>
      ) : null}

      {status?.last_error ? <p className="job-error">Last error: {status.last_error}</p> : null}

      {status ? (
        <p className="job-times">
          {status.provider} · {status.model}
          {status.key_present ? "" : " · no API key"}
          {` · updated ${formatDateTime(status.updated_at)}`}
        </p>
      ) : null}
    </section>
  );
}
