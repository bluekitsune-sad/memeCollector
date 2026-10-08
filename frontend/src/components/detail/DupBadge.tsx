"use client";

import { daysUntil, dupLabel, formatCount } from "@/lib/format";
import type { MediaDetail } from "@/lib/types";

interface DupBadgeProps {
  item: MediaDetail;
  busy?: boolean;
  onUnflag: () => void;
}

/**
 * Dup status badge with the 7-day auto-delete countdown (PRD §12.1): a `dup`
 * item shows when it expires and offers "Unflag" to rescue it.
 */
export default function DupBadge({ item, busy = false, onUnflag }: DupBadgeProps) {
  const status = item.dup_status;
  const days = status === "dup" ? daysUntil(item.dup_expires_at) : null;

  return (
    <div className="badge-row">
      <span className={`badge ${status}`} title="Duplicate scan status">
        {dupLabel(status)}
      </span>

      {status === "dup" ? (
        <>
          <span className="inline-msg error">
            Pending deletion
            {days !== null ? ` · ${formatCount(days)} day${days === 1 ? "" : "s"} left` : ""}
            {item.dup_expires_at ? ` (expires ${item.dup_expires_at.slice(0, 10)})` : ""}
          </span>
          <button type="button" className="btn small" onClick={onUnflag} disabled={busy}>
            Unflag
          </button>
        </>
      ) : null}

      {item.dup_of_media_id ? (
        <span className="inline-msg">duplicate of media #{item.dup_of_media_id}</span>
      ) : null}
    </div>
  );
}
