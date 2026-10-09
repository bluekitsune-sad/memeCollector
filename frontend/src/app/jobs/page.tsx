import type { Metadata } from "next";
import { Suspense } from "react";
import AiStatusCard from "@/components/jobs/AiStatusCard";
import BackfillCard from "@/components/jobs/BackfillCard";
import JobsList from "@/components/jobs/JobsList";
import { LoadingRow } from "@/components/StateBlocks";

export const metadata: Metadata = {
  title: "Jobs",
  description: "Background job progress and crawl controls (PRD §35).",
};

export default function JobsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Jobs</h1>
      </div>
      <BackfillCard />
      <AiStatusCard />
      <Suspense fallback={<LoadingRow label="Loading jobs…" />}>
        <JobsList />
      </Suspense>
    </>
  );
}
