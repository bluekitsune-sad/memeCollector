import type { Metadata } from "next";
import AddSource from "@/components/add/AddSource";

export const metadata: Metadata = {
  title: "Add Source",
  description: "Start a crawl on a supported comic site (PRD §5.1–§5.2).",
};

export default function AddSourcePage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Add Source</h1>
        <span className="page-sub">Collect comment media from a supported comic site</span>
      </div>
      <AddSource />
    </>
  );
}
