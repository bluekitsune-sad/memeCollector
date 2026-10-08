import type { Metadata } from "next";
import { Suspense } from "react";
import SettingsForm from "@/components/settings/SettingsForm";
import { LoadingRow } from "@/components/StateBlocks";

export const metadata: Metadata = {
  title: "Settings",
  description: "Crawl limits, AI provider, privacy and responsible-use notices.",
};

export default function SettingsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Settings</h1>
      </div>
      <Suspense fallback={<LoadingRow label="Loading settings…" />}>
        <SettingsForm />
      </Suspense>
    </>
  );
}
