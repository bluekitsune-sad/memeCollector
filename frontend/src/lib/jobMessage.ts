/** Parsing for `jobs.message` — the `key=value` counters of PRD §5.2. */

export interface Counter {
  key: string;
  value: string;
  /** Set for `key=done/total` tokens (e.g. `pages=37/100`). */
  current?: number;
  total?: number;
}

export interface JobMessage {
  /** Counters in display order. */
  counters: Counter[];
  /** Bare tokens such as `paused` or `limit_reached`. */
  flags: string[];
  /** Original text, shown as a fallback when nothing parses. */
  raw: string;
}

/** Display order for the scan screen (PRD §5.2). */
const ORDER = ["pages", "comments", "media", "new", "dup", "failed", "skipped", "page_errors"];

const LABELS: Record<string, string> = {
  pages: "Pages scanned",
  comments: "Comments discovered",
  media: "Media found",
  new: "New media",
  dup: "Duplicates",
  failed: "Failed downloads",
  skipped: "Pages skipped",
  page_errors: "Page errors",
};

export function counterLabel(key: string): string {
  return LABELS[key] ?? key.replace(/_/g, " ");
}

/** Split a job message into counters + bare flags; unparseable text stays visible. */
export function parseJobMessage(message: string | null): JobMessage {
  const raw = (message ?? "").trim();
  const counters: Counter[] = [];
  const flags: string[] = [];
  for (const token of raw.split(/\s+/)) {
    if (token === "") continue;
    const separator = token.indexOf("=");
    if (separator === -1) {
      flags.push(token);
      continue;
    }
    const key = token.slice(0, separator);
    const value = token.slice(separator + 1);
    if (key === "") continue;
    const ratio = /^(\d+)\/(\d+)$/.exec(value);
    if (ratio) {
      counters.push({ key, value, current: Number(ratio[1]), total: Number(ratio[2]) });
    } else if (/^\d+$/.test(value)) {
      counters.push({ key, value, current: Number(value) });
    } else {
      counters.push({ key, value });
    }
  }
  counters.sort((a, b) => {
    const ai = ORDER.indexOf(a.key);
    const bi = ORDER.indexOf(b.key);
    return (ai === -1 ? ORDER.length : ai) - (bi === -1 ? ORDER.length : bi);
  });
  return { counters, flags, raw };
}
