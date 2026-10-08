"use client";

import JobControls from "./JobControls";
import ProgressBar, { progressPercent } from "./ProgressBar";
import ScanCounters from "./ScanCounters";
import { formatDateTime } from "@/lib/format";
import type { Job, JobAction } from "@/lib/types";

const STATUS_TONE: Record<string, string> = {
  running: "processing",
  completed: "ready",
  failed: "failed",
  cancelled: "unflagged",
};

interface JobCardProps {
  job: Job;
  busyId: number | null;
  onAction: (jobId: number, action: JobAction) => void;
}

/** One job row: status, progress, counters, errors, times, controls (PRD §35). */
export default function JobCard({ job, busyId, onAction }: JobCardProps) {
  return (
    <article className="job-row">
      <div className="job-head">
        <span className="job-title">
          <i className={`status-icon ${job.status}`} aria-hidden="true" />
          {job.job_type} <span className="type">#{job.id}</span>
        </span>
        <div className="badge-row">
          <span className={`badge ${STATUS_TONE[job.status] ?? ""}`}>{job.status}</span>
          <JobControls job={job} busyId={busyId} onAction={onAction} />
        </div>
      </div>

      <ProgressBar progress={job.progress} label={`${job.job_type} #${job.id} progress`} />
      <p className="job-message">
        <b>{progressPercent(job.progress)}%</b>
      </p>

      <ScanCounters message={job.message} />

      {job.error ? <p className="job-error">Error: {job.error}</p> : null}

      <p className="job-times">
        Created {formatDateTime(job.created_at)}
        {job.completed_at ? ` · Completed ${formatDateTime(job.completed_at)}` : ""}
      </p>
    </article>
  );
}
