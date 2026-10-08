"use client";

import { useState } from "react";
import SettingField from "./SettingField";
import SettingsInfo from "./SettingsInfo";
import type { Feedback } from "@/components/detail/DetailActions";
import { ErrorPanel, LoadingRow } from "@/components/StateBlocks";
import { errorMessage, getSettings, updateSettings } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import {
  aiFormFrom,
  crawlError,
  crawlFormFrom,
  settingsPatch,
  type AiForm,
  type CrawlForm,
} from "@/lib/settingsForm";

/** Settings form: crawl limits + AI block, with read-only info below (PRD §42–§43). */
export default function SettingsForm() {
  const { data: settings, error, loading, reload, mutate } = useAsync(() => getSettings(), "settings");
  const [formOverride, setFormOverride] = useState<Partial<CrawlForm>>({});
  const [aiOverride, setAiOverride] = useState<Partial<AiForm>>({});
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  async function save(): Promise<void> {
    if (!settings) return;
    const form = crawlFormFrom(settings, formOverride);
    const ai = aiFormFrom(settings, aiOverride);
    const problem = crawlError(form);
    if (problem) {
      setFeedback({ kind: "error", text: problem });
      return;
    }
    setBusy(true);
    setFeedback(null);
    try {
      mutate(await updateSettings(settingsPatch(form, ai)));
      setFormOverride({});
      setAiOverride({});
      setFeedback({ kind: "ok", text: "Settings saved." });
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  if (loading && !settings) return <LoadingRow label="Loading settings…" />;
  if (!settings) {
    return <ErrorPanel message={error ?? "Settings unavailable"} onRetry={reload} />;
  }

  const form = crawlFormFrom(settings, formOverride);
  const ai = aiFormFrom(settings, aiOverride);
  const keyPresent = settings.ai.key_present === true;
  const setCrawl = (key: keyof CrawlForm) => (value: string) =>
    setFormOverride((previous) => ({ ...previous, [key]: value }));
  const setAi = (key: keyof AiForm) => (value: string) =>
    setAiOverride((previous) => ({ ...previous, [key]: value }));

  return (
    <>
      <section className="panel">
        <h2>Crawl limits</h2>
        <div className="form-grid">
          <SettingField
            id="crawl-delay"
            label="Delay between pages (seconds)"
            type="number"
            min={1}
            step={0.5}
            value={form.delay_seconds}
            onChange={setCrawl("delay_seconds")}
          />
          <SettingField
            id="crawl-concurrency"
            label="Concurrency (1–2 recommended)"
            type="number"
            min={1}
            max={8}
            step={1}
            value={form.concurrency}
            onChange={setCrawl("concurrency")}
          />
          <SettingField
            id="crawl-max-pages"
            label="Max pages per crawl"
            type="number"
            min={0}
            step={1}
            value={form.max_pages}
            onChange={setCrawl("max_pages")}
          />
          <SettingField
            id="crawl-download-limit"
            label="Download limit per crawl"
            type="number"
            min={0}
            step={1}
            value={form.download_limit}
            onChange={setCrawl("download_limit")}
          />
          <SettingField
            id="crawl-max-file"
            label="Max file size (MB)"
            type="number"
            min={1}
            step={1}
            value={form.max_file_size_mb}
            onChange={setCrawl("max_file_size_mb")}
          />
        </div>
      </section>

      <section className="panel">
        <h2>AI analysis</h2>

        {settings.ai.external_provider ? (
          <p className="notice privacy" role="note">
            {settings.notices?.privacy ??
              "Selected media is sent to an external AI provider — it does not stay on this machine."}
          </p>
        ) : null}

        <p className="badge-row" style={{ margin: "10px 0" }}>
          <span className={`badge ${keyPresent ? "ready" : "failed"}`}>
            API key: {keyPresent ? "configured" : "not configured"}
          </span>
          <span className="inline-msg">
            {settings.ai.external_provider
              ? "Selected media is sent to an external provider for analysis."
              : "Analysis stays local (no external provider configured)."}
          </span>
        </p>

        <div className="form-grid">
          <SettingField
            id="ai-provider"
            label="Provider"
            type="text"
            value={ai.provider}
            onChange={setAi("provider")}
          />
          <SettingField
            id="ai-model"
            label="Vision model"
            type="text"
            value={ai.model}
            onChange={setAi("model")}
          />
          <SettingField
            id="ai-embedding"
            label="Embedding model"
            type="text"
            value={ai.embedding_model}
            onChange={setAi("embedding_model")}
          />
        </div>
      </section>

      <div className="form-actions">
        <button type="button" className="btn primary" onClick={() => void save()} disabled={busy}>
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          Save settings
        </button>
        {feedback ? (
          <span className={`inline-msg ${feedback.kind}`} role="status">
            {feedback.text}
          </span>
        ) : null}
        {error ? <span className="inline-msg error">{error}</span> : null}
      </div>

      <SettingsInfo settings={settings} />
    </>
  );
}
