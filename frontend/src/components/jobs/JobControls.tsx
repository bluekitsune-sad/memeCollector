"use client";

import { errorMessage, jobAction } from "@/lib/api";
import type { Job, JobAction } from "@/lib/types";
import { useState } from "react";

export interface JobActionsState {
  /** Job currently running an action, if any. */
  busyId: number | null;
  note: { kind: "ok" | "error"; text: string } | null;
  act: (jobId: number, action: JobAction) => Promise<void>;
}

/**
 * Pause / resume / cancel for live crawl jobs (PRD §5.2, §35). The backend is
 * no-op-safe for finished or non-crawl jobs, and says so via `applied`.
 */
export function useJobActions(onChanged: () => void): JobActionsState {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [note, setNote] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  async function act(jobId: number, action: JobAction): Promise<void> {
    setBusyId(jobId);
    setNote(null);
    try {
      const result = await jobAction(jobId, action);
      setNote({
        kind: result.applied ? "ok" : "error",
        text: result.applied
          ? `Job ${action}ed.`
          : `Cannot ${action} this job (not a live crawl).`,
      });
      onChanged();
    } catch (caught) {
      setNote({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusyId(null);
    }
  }

  return { busyId, note, act };
}

interface JobControlsProps {
  job: Job;
  busyId: number | null;
  onAction: (jobId: number, action: JobAction) => void;
}

/** Pause / Resume / Cancel buttons — only meaningful on a running crawl. */
export default function JobControls({ job, busyId, onAction }: JobControlsProps) {
  const isRunningCrawl = job.job_type === "crawl" && job.status === "running";
  if (!isRunningCrawl) return null;
  const paused = job.message === "paused";
  const busy = busyId === job.id;

  return (
    <div className="job-actions">
      {paused ? (
        <button type="button" className="btn small" disabled={busy} onClick={() => onAction(job.id, "resume")}>
          Resume
        </button>
      ) : (
        <button type="button" className="btn small" disabled={busy} onClick={() => onAction(job.id, "pause")}>
          Pause
        </button>
      )}
      <button type="button" className="btn small danger" disabled={busy} onClick={() => onAction(job.id, "cancel")}>
        Cancel
      </button>
    </div>
  );
}
