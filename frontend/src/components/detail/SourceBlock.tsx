import { formatDate, formatDateTime } from "@/lib/format";
import type { MediaSource } from "@/lib/types";

interface SourceBlockProps {
  sources: MediaSource[];
  collectedAt: string;
}

/** Provenance block — every field required by PRD §31. */
export default function SourceBlock({ sources, collectedAt }: SourceBlockProps) {
  const primary = sources[0];

  if (!primary) {
    return (
      <section className="panel">
        <h2>Source</h2>
        <p className="inline-msg">No provenance recorded for this item.</p>
      </section>
    );
  }

  return (
    <section className="panel">
      <h2>Source</h2>
      <dl className="kv">
        <dt>Website</dt>
        <dd>{primary.site}</dd>
        <dt>Chapter</dt>
        <dd>{primary.chapter ?? "—"}</dd>
        <dt>Page</dt>
        <dd>{primary.page_number ?? "—"}</dd>
        <dt>Comment ID</dt>
        <dd>{primary.comment_id ?? "—"}</dd>
        <dt>Author</dt>
        <dd>{primary.author_name ?? "—"}</dd>
        <dt>Original URL</dt>
        <dd>
          <a href={primary.page_url} target="_blank" rel="noreferrer noopener">
            {primary.page_url}
          </a>
        </dd>
        <dt>Media URL</dt>
        <dd>
          <a href={primary.media_url} target="_blank" rel="noreferrer noopener">
            {primary.media_url}
          </a>
        </dd>
        <dt>Collected</dt>
        <dd>{formatDate(primary.collected_at ?? collectedAt)}</dd>
        <dt>Added to library</dt>
        <dd>{formatDateTime(collectedAt)}</dd>
      </dl>

      <div className="action-row" style={{ marginTop: 12 }}>
        <a className="btn" href={primary.page_url} target="_blank" rel="noreferrer noopener">
          Open Source ↗
        </a>
      </div>

      {sources.length > 1 ? (
        <p className="inline-msg" style={{ marginTop: 8 }}>
          Also seen on {sources.length - 1} other page{sources.length - 1 === 1 ? "" : "s"}.
        </p>
      ) : null}
    </section>
  );
}
