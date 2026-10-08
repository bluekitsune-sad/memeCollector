/** Small presentation helpers shared across pages (no business logic). */

import type { MediaItem } from "./types";

const DATE_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

const NUMBER_FORMAT = new Intl.NumberFormat();

/** Backend timestamps are UTC `YYYY-MM-DD HH:MM:SS` text — parse them as UTC. */
export function parseUtc(value: string): Date | null {
  const normalized = value.trim().replace(" ", "T");
  const withZone = /Z|[+-]\d{2}:\d{2}$/.test(normalized) ? normalized : `${normalized}Z`;
  const date = new Date(withZone);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `2026-10-06 12:34:56` → locale date + time. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = parseUtc(value);
  return date ? DATE_TIME_FORMAT.format(date) : value;
}

/** `2026-10-06 12:34:56` → locale date only. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = parseUtc(value);
  return date ? DATE_FORMAT.format(date) : value;
}

/** Whole days left before `value` (never negative). */
export function daysUntil(value: string | null | undefined): number | null {
  if (!value) return null;
  const date = parseUtc(value);
  if (!date) return null;
  const ms = date.getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / 86_400_000));
}

export function formatCount(value: number | undefined | null): string {
  return value === undefined || value === null ? "—" : NUMBER_FORMAT.format(value);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

/** Display label for a processing status (PRD §19). */
export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    DOWNLOADED: "Downloaded",
    ANALYZING: "Analyzing",
    ANALYZED: "Analyzed",
    EMBEDDING: "Embedding",
    READY: "Ready",
    FAILED: "Failed",
  };
  return labels[status] ?? status;
}

export function dupLabel(status: string): string {
  const labels: Record<string, string> = {
    dup: "Duplicate",
    nondup: "Unique",
    unflagged: "Unflagged",
  };
  return labels[status] ?? status;
}

/** PRD §19 grouping: READY vs. in-flight vs. failed. */
export function statusTone(status: string): "ready" | "processing" | "failed" {
  if (status === "READY") return "ready";
  if (status === "FAILED") return "failed";
  return "processing";
}

/** Which kind of media an item is, derived from its extension / MIME type. */
export function mediaKind(item: Pick<MediaItem, "extension" | "mime_type">): "image" | "gif" | "video" {
  const extension = (item.extension ?? "").toLowerCase();
  const mime = (item.mime_type ?? "").toLowerCase();
  if (extension === "gif") return "gif";
  if (mime.startsWith("video/") || ["mp4", "webm", "mov", "mkv"].includes(extension)) return "video";
  return "image";
}
