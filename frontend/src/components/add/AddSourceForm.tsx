"use client";

import { FormEvent, useState } from "react";
import { errorMessage, startScrape } from "@/lib/api";
import type { ScrapeScope } from "@/lib/types";

const SCOPES: { value: ScrapeScope; label: string; needsUrls: boolean }[] = [
  { value: "current_page", label: "Current page", needsUrls: false },
  { value: "current_chapter", label: "Current chapter", needsUrls: false },
  { value: "multiple_chapters", label: "Multiple chapters", needsUrls: true },
  { value: "entire_comic", label: "Entire comic", needsUrls: false },
  { value: "custom_urls", label: "Custom list of URLs", needsUrls: true },
];

interface AddSourceFormProps {
  onStarted: (jobId: number) => void;
}

/** URL + scope form that starts a crawl (PRD §5.1, §0 unsupported-site error). */
export default function AddSourceForm({ onStarted }: AddSourceFormProps) {
  const [url, setUrl] = useState("");
  const [scope, setScope] = useState<ScrapeScope>("current_page");
  const [urlsText, setUrlsText] = useState("");
  const [forceRescan, setForceRescan] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const needsUrls = SCOPES.find((entry) => entry.value === scope)?.needsUrls ?? false;

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const started = await startScrape({
        url: url.trim(),
        scope,
        force_rescan: forceRescan,
        urls: urlsText
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter((line) => line !== ""),
      });
      onStarted(started.job_id);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel" onSubmit={(event) => void submit(event)}>
      <h2>New crawl</h2>

      {error ? (
        <div className="error-panel" role="alert">
          <span>{error}</span>
        </div>
      ) : null}

      <div className="form-grid">
        <div className="field">
          <label htmlFor="source-url">Comic URL</label>
          <input
            id="source-url"
            className="input"
            type="url"
            required
            placeholder="https://asurascans.com/comic/…/chapter-42/page-17"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="source-scope">Scope</label>
          <select
            id="source-scope"
            className="select"
            value={scope}
            onChange={(event) => setScope(event.target.value as ScrapeScope)}
          >
            {SCOPES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {needsUrls ? (
        <div className="field" style={{ marginTop: 14 }}>
          <label htmlFor="source-urls">
            {scope === "multiple_chapters" ? "Chapter URLs (one per line)" : "URL list (one per line)"}
          </label>
          <textarea
            id="source-urls"
            className="textarea"
            placeholder={"https://example.com/comic/chapter-1\nhttps://example.com/comic/chapter-2"}
            value={urlsText}
            onChange={(event) => setUrlsText(event.target.value)}
          />
        </div>
      ) : null}

      <div className="form-actions">
        <label className="inline-msg" htmlFor="force-rescan">
          <input
            id="force-rescan"
            type="checkbox"
            checked={forceRescan}
            onChange={(event) => setForceRescan(event.target.checked)}
          />{" "}
          Re-scan pages I already collected
        </label>
        <button className="btn primary" type="submit" disabled={busy || url.trim() === ""}>
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          Start scan
        </button>
      </div>

      <p className="inline-msg" style={{ marginTop: 12 }}>
        Supported sites: asurascans.com, mangadex.org, mangapark.net, comix.to — anything else is
        rejected with an explicit message.
      </p>
    </form>
  );
}
