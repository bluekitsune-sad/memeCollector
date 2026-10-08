"use client";

import Link from "next/link";
import JobControls, { useJobActions } from "@/components/jobs/JobControls";
import ProgressBar, { progressPercent } from "@/components/jobs/ProgressBar";
import ScanCounters from "@/components/jobs/ScanCounters";
import { ErrorPanel, LoadingRow } from "@/components/StateBlocks";
import { getScrapeStatus } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { formatDateTime } from "@/lib/format";
import type { Job } from "@/lib/types";

const TERMINAL = ["completed", "failed", "cancelled"];

function isFinished(job: Job): boolean {
  return TERMINAL.includes(job.status);
}

interface JobProgressViewProps {
  jobId: number;
  onBack: () => void;
}

/** Live scan progress after `POST /api/scrape` (PRD §5.2), polled every 1.5s. */
export default function JobProgressView({ jobId, onBack }: JobProgressViewProps) {
  const { data: job, error, loading, reload } = usePoll(
    () => getScrapeStatus(jobId),
    1500,
    true,
    String(jobId),
    isFinished,
  );
  const { busyId, note, act } = useJobActions(reload);

  if (loading && !job) return <LoadingRow label="Starting scan…" />;
  if (!job) return <ErrorPanel message={error ?? "Job not found"} onRetry={reload} />;

  const done = isFinished(job);

  return (
    <section className="panel">
      <div className="job-head">
        <span className="job-title">
          <i className={`status-icon ${job.status}`} aria-hidden="true" />
          {done ? "Scan finished" : "Scanning…"} <span className="type">job #{job.id}</span>
        </span>
        <span
          className={`badge ${job.status === "completed" ? "ready" : job.status === "failed" ? "failed" : "processing"}`}
        >
          {job.status}
        </span>
      </div>

      <ProgressBar progress={job.progress} label="Scan progress" />
      <p className="job-message">
        <b>{progressPercent(job.progress)}%</b> · started{" "}
        {formatDateTime(job.started_at ?? job.created_at)}
      </p>

      <ScanCounters message={job.message} />

      {job.error ? <p className="job-error">Error: {job.error}</p> : null}
      {error ? <p className="job-error">{error}</p> : null}
      {note ? (
        <p className={`inline-msg ${note.kind}`} role="status">
          {note.text}
        </p>
      ) : null}

      <div className="action-row" style={{ marginTop: 14 }}>
        <JobControls job={job} busyId={busyId} onAction={(id, action) => void act(id, action)} />
        <button type="button" className="btn" onClick={onBack}>
          New scan
        </button>
        <Link className="btn" href="/jobs">
          All jobs
        </Link>
        {job.status === "completed" ? (
          <Link className="btn primary" href="/">
            View gallery
          </Link>
        ) : null}
      </div>
    </section>
  );
}
