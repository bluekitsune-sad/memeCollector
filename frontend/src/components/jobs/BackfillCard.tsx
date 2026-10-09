"use client";

import { useState } from "react";
import ProgressBar, { progressPercent } from "./ProgressBar";
import {
  backfillAction,
  errorMessage,
  getBackfill,
  listBackfillItems,
  listBackfills,
  startBackfill,
} from "@/lib/api";
import { formatCount, formatDateTime } from "@/lib/format";
import { useAsync, usePoll } from "@/lib/hooks";
import { EmptyState, ErrorPanel, LoadingRow } from "@/components/StateBlocks";
import type { BackfillItemStatus, BackfillStatus } from "@/lib/types";

/** Poll cadence for the live card (matches the other Jobs-page cards). */
const POLL_MS = 3000;

/** Badge tone per run status (mirrors JobCard). */
const STATUS_TONE: Record<BackfillStatus, string> = {
  running: "processing",
  completed: "ready",
  failed: "failed",
  cancelled: "unflagged",
};

/** Marker glyph + badge tone per comic status. */
const ITEM_MARKER: Record<BackfillItemStatus, { glyph: string; tone: string }> = {
  done: { glyph: "✓", tone: "ready" },
  failed: { glyph: "✗", tone: "failed" },
  running: { glyph: "◷", tone: "processing" },
  pending: { glyph: "○", tone: "unflagged" },
};

interface Note {
  kind: "ok" | "error";
  text: string;
}

/** Site-wide backfill card: polls `/api/backfill` and drives the latest run. */
export default function BackfillCard() {
  const list = usePoll(() => listBackfills(10), POLL_MS, true, "backfill");
  const latest = list.data?.items[0] ?? null;
  const detail = usePoll(
    () => getBackfill(latest?.id ?? 0),
    POLL_MS,
    latest !== null,
    `backfill-detail-${latest?.id ?? 0}`,
  );

  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note | null>(null);
  const [showComics, setShowComics] = useState(false);

  const run = detail.data ?? latest;
  const fraction = run && run.total > 0 ? (run.done + run.failed) / run.total : 0;
  const counts = run
    ? (detail.data?.counts ?? {
        pending: Math.max(0, run.total - run.done - run.failed - (run.status === "running" ? 1 : 0)),
        running: run.status === "running" ? 1 : 0,
        done: run.done,
        failed: run.failed,
      })
    : null;

  function refresh(): void {
    list.reload();
    detail.reload();
  }

  async function start(): Promise<void> {
    setBusy(true);
    setNote(null);
    try {
      await startBackfill();
      setNote({ kind: "ok", text: "Backfill started." });
      refresh();
    } catch (caught) {
      // 409 ("already running") and 400 (disabled/unsupported) surface inline.
      setNote({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function act(action: "pause" | "resume" | "cancel" | "retry-failed"): Promise<void> {
    if (!run) return;
    setBusy(true);
    setNote(null);
    try {
      const result = await backfillAction(run.id, action);
      const done = action === "retry-failed" ? "will retry failed comics" : `${action}d`;
      setNote({
        kind: result.applied ? "ok" : "error",
        text: result.applied
          ? `Backfill ${done}.`
          : `Cannot ${action} this backfill right now (not in a compatible state).`,
      });
      refresh();
    } catch (caught) {
      setNote({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  const loadingList = list.loading && list.data === null;

  return (
    <section className="panel" aria-live="polite">
      <div className="job-head">
        <span className="job-title">
          <i className={`status-icon ${run?.status ?? "paused"}`} aria-hidden="true" />
          Site Backfill {run ? <span className="type">#{run.id}</span> : null}
        </span>
        {run ? (
          <div className="badge-row">
            {run.live ? <span className="badge processing">live</span> : null}
            <span className={`badge ${STATUS_TONE[run.status]}`}>{run.status}</span>
          </div>
        ) : null}
      </div>

      {list.error && run === null ? <ErrorPanel message={list.error} onRetry={list.reload} /> : null}
      {loadingList ? <LoadingRow label="Loading backfill…" /> : null}
      {!loadingList && list.error === null && run === null ? (
        <EmptyState
          title="No backfill yet"
          hint="Walks the whole AsuraScans catalog one comic at a time and keeps going until done."
          action={
            <button type="button" className="btn primary" disabled={busy} onClick={() => void start()}>
              Start site-wide backfill
            </button>
          }
        />
      ) : null}

      {run && counts ? (
        <>
          <p className="job-message">
            <a href={run.site_url} target="_blank" rel="noreferrer">
              {run.site_url}
            </a>
            {run.adapter_site ? ` · ${run.adapter_site}` : ""}
          </p>

          <ProgressBar progress={fraction} label="Site backfill progress" />
          <p className="job-message">
            <b>{progressPercent(fraction)}%</b>
            {` · ${run.done + run.failed} / ${run.total} comics`}
          </p>

          <div className="counters">
            <div className="counter">
              <div className="label">Pending</div>
              <div className="value">{formatCount(counts.pending)}</div>
            </div>
            <div className="counter">
              <div className="label">Running</div>
              <div className="value">{formatCount(counts.running)}</div>
            </div>
            <div className="counter">
              <div className="label">Done</div>
              <div className="value">{formatCount(counts.done)}</div>
            </div>
            <div className="counter">
              <div className="label">Failed</div>
              <div className="value">{formatCount(counts.failed)}</div>
            </div>
          </div>

          {run.status === "running" ? (
            <p className="job-message">
              Current:{" "}
              {run.current_url ? (
                <a href={run.current_url} target="_blank" rel="noreferrer">
                  {run.current_title ?? run.current_url}
                </a>
              ) : (
                run.current_title ?? "—"
              )}
            </p>
          ) : null}

          {run.message ? <p className="job-message">{run.message}</p> : null}
          {run.error ? <p className="job-error">Error: {run.error}</p> : null}

          <div className="job-actions">
            {run.status === "running" && run.live ? (
              <button type="button" className="btn small" disabled={busy} onClick={() => void act("pause")}>
                Pause
              </button>
            ) : null}
            {run.status === "running" && !run.live ? (
              <button type="button" className="btn small" disabled={busy} onClick={() => void act("resume")}>
                Resume
              </button>
            ) : null}
            {run.status === "running" ? (
              <button type="button" className="btn small danger" disabled={busy} onClick={() => void act("cancel")}>
                Cancel
              </button>
            ) : null}
            {run.failed > 0 ? (
              <button type="button" className="btn small" disabled={busy} onClick={() => void act("retry-failed")}>
                Retry failed
              </button>
            ) : null}
            {run.status !== "running" ? (
              <button type="button" className="btn primary small" disabled={busy} onClick={() => void start()}>
                Run again
              </button>
            ) : null}
          </div>

          {note ? (
            <p className={`inline-msg ${note.kind}`} role="status">
              {note.text}
            </p>
          ) : null}

          <p className="job-times">
            Started {formatDateTime(run.started_at)}
            {run.completed_at ? ` · Completed ${formatDateTime(run.completed_at)}` : ""}
          </p>

          <div className="badge-row">
            <button
              type="button"
              className="btn small"
              aria-expanded={showComics}
              onClick={() => setShowComics((value) => !value)}
            >
              {showComics ? "Hide comics" : `Comics (${formatCount(run.total)})`}
            </button>
          </div>
          {showComics ? (
            <BackfillItems backfillId={run.id} progressKey={`${run.done}-${run.failed}`} />
          ) : null}
        </>
      ) : null}
    </section>
  );
}

/** Per-comic list; the key embeds the run's counts so it refetches as they move. */
function BackfillItems({ backfillId, progressKey }: { backfillId: number; progressKey: string }) {
  const { data, error, loading, reload } = useAsync(
    () => listBackfillItems(backfillId, { limit: 500 }),
    `backfill-items-${backfillId}-${progressKey}`,
  );

  const items = data?.items ?? [];

  return (
    <div className="job-list">
      {error && items.length === 0 ? <ErrorPanel message={error} onRetry={reload} /> : null}
      {loading && items.length === 0 ? <LoadingRow label="Loading comics…" /> : null}
      {!loading && !error && items.length === 0 ? (
        <p className="inline-msg">No comics queued yet.</p>
      ) : null}

      {items.map((item) => {
        const marker = ITEM_MARKER[item.status];
        return (
          <div className="job-row" key={item.id}>
            <span className="job-title">
              <span className={`badge ${marker.tone}`} aria-hidden="true">
                {marker.glyph}
              </span>
              <a href={item.url} target="_blank" rel="noreferrer">
                {item.title ?? item.url}
              </a>
            </span>
            {item.error ? <p className="job-error">{item.error}</p> : null}
          </div>
        );
      })}

      {data && items.length < data.total ? (
        <p className="inline-msg">
          Showing {formatCount(items.length)} of {formatCount(data.total)} comics.
        </p>
      ) : null}
    </div>
  );
}
