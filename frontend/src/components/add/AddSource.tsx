"use client";

import { useState } from "react";
import AddSourceForm from "./AddSourceForm";
import JobProgressView from "./JobProgressView";

/** Add Source: crawl form, then the live progress view for the started job. */
export default function AddSource() {
  const [jobId, setJobId] = useState<number | null>(null);

  if (jobId !== null) {
    return <JobProgressView jobId={jobId} onBack={() => setJobId(null)} />;
  }
  return <AddSourceForm onStarted={setJobId} />;
}
