"use client";

import { useState } from "react";
import JobCard from "./JobCard";
import { useJobActions } from "./JobControls";
import { EmptyState, ErrorPanel, LoadingRow } from "@/components/StateBlocks";
import { listJobs } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "running", label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

/** Jobs history with auto-refresh (PRD §35). */
export default function JobsList() {
  const [statusFilter, setStatusFilter] = useState("");
  const { data, error, loading, reload } = usePoll(
    () => listJobs({ status: statusFilter || undefined, limit: 50 }),
    3000,
    true,
    statusFilter,
  );
  const { busyId, note, act } = useJobActions(reload);

  const jobs = data?.items ?? [];

  return (
    <>
      <div className="toolbar">
        <div className="field" style={{ minWidth: 180 }}>
          <label htmlFor="job-status">Status</label>
          <select
            id="job-status"
            className="select"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="badge-row">
          <span className="inline-msg">Auto-refreshes every 3s</span>
          <button type="button" className="btn small" onClick={reload}>
            Refresh
          </button>
        </div>
      </div>

      {note ? (
        <p className={`inline-msg ${note.kind}`} role="status">
          {note.text}
        </p>
      ) : null}

      {error && jobs.length === 0 ? <ErrorPanel message={error} onRetry={reload} /> : null}
      {loading && jobs.length === 0 ? <LoadingRow label="Loading jobs…" /> : null}
      {!loading && !error && jobs.length === 0 ? (
        <EmptyState title="No jobs yet" hint="Start a crawl from Add Source to see progress here." />
      ) : null}

      <div className="job-list">
        {jobs.map((job) => (
          <JobCard key={job.id} job={job} busyId={busyId} onAction={(id, action) => void act(id, action)} />
        ))}
      </div>
    </>
  );
}
