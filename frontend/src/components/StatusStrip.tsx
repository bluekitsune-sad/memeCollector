import { formatCount } from "@/lib/format";
import type { StatusCounts } from "@/lib/types";

interface StatusStripProps {
  total: number;
  /** PRD §19 breakdown; `—` while the backend does not report it. */
  statusCounts?: StatusCounts;
  /** PRD §12.1 dup breakdown, keyed by dup/nondup/unflagged. */
  dupCounts?: Record<string, number>;
  /** Currently applied dup filter ("" = all). */
  dupFilter?: string;
  /** Present only where a dup filter applies (gallery). */
  onDupFilter?: (value: string) => void;
}

/** Header summary strip: Total / Ready / Processing / Failed + dup badges (PRD §19, §12.1). */
export default function StatusStrip({
  total,
  statusCounts,
  dupCounts,
  dupFilter,
  onDupFilter,
}: StatusStripProps) {
  const dupKeys: { key: string; label: string }[] = [
    { key: "dup", label: "Dup" },
    { key: "nondup", label: "Non-dup" },
    { key: "unflagged", label: "Unflagged" },
  ];

  return (
    <div className="status-strip" aria-label="Library status">
      <span className="stat">
        Total <b>{formatCount(total)}</b>
      </span>
      <span className="stat">
        <i className="dot ready" aria-hidden="true" />
        Ready <b>{formatCount(statusCounts?.ready)}</b>
      </span>
      <span className="stat">
        <i className="dot processing" aria-hidden="true" />
        Processing <b>{formatCount(statusCounts?.processing)}</b>
      </span>
      <span className="stat">
        <i className="dot failed" aria-hidden="true" />
        Failed <b>{formatCount(statusCounts?.failed)}</b>
      </span>

      {dupKeys.map(({ key, label }) => {
        if (!dupCounts) return null;
        const count = dupCounts[key];
        const content = (
          <>
            <i className={`dot ${key}`} aria-hidden="true" />
            {label} <b>{formatCount(count)}</b>
          </>
        );
        if (!onDupFilter) return <span className="stat" key={key}>{content}</span>;
        const selected = (dupFilter ?? "") === key;
        return (
          <button
            key={key}
            type="button"
            className={`stat${selected ? " selected" : ""}`}
            aria-pressed={selected}
            onClick={() => onDupFilter(selected ? "" : key)}
            title={`Filter by ${label.toLowerCase()} items`}
          >
            {content}
          </button>
        );
      })}
    </div>
  );
}
