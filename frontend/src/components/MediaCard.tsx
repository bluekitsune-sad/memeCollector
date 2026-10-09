"use client";

import Link from "next/link";
import { useState } from "react";
import { mediaFileUrl } from "@/lib/api";
import { mediaKind, statusTone } from "@/lib/format";
import type { MediaItem } from "@/lib/types";

/** Gallery item plus an optional relevance score (search results, PRD §23). */
export type MediaCardData = MediaItem & { score?: number };

interface MediaCardProps {
  item: MediaCardData;
  /** Show the ranking score badge (search results only). */
  showScore?: boolean;
}

/** One lazy-loaded thumbnail card (PRD §25, §50 — never load originals). */
export default function MediaCard({ item, showScore = false }: MediaCardProps) {
  const [thumbFailed, setThumbFailed] = useState(false);
  const kind = mediaKind(item);
  const tone = statusTone(item.processing_status);
  const alt = item.title ?? item.original_filename ?? `Media ${item.id}`;
  const score = typeof item.score === "number" ? Math.round(item.score * 100) : null;

  return (
    <Link href={`/media/${item.id}`} className="card" aria-label={`Open ${alt}`}>
      <div className="card-media">
        {thumbFailed ? (
          <span className="card-placeholder">
            {tone === "failed" ? "Analysis failed" : "Preview not generated yet"}
          </span>
        ) : (
          // Plain <img> on purpose: local /api proxy target, and GIF thumbs must animate.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={mediaFileUrl(item.id, "thumbnail")}
            alt={alt}
            loading="lazy"
            decoding="async"
            className="pixel-art"
            onError={() => setThumbFailed(true)}
          />
        )}

        <span className="card-badges">
          {kind === "gif" ? <span className="badge gif">GIF</span> : null}
          {tone === "failed" ? <span className="badge failed">Failed</span> : null}
          {tone === "processing" ? <span className="badge processing">…</span> : null}
          {item.dup_status === "dup" ? <span className="badge dup">Dup</span> : null}
        </span>

        {item.is_favorite ? (
          <span className="card-heart" title="Favorite" aria-hidden="true">
            ❤️
          </span>
        ) : null}

        {showScore && score !== null ? (
          <span className="card-score" title="Relevance score">
            {score}%
          </span>
        ) : null}
      </div>

      <span className="card-body">
        <span className="card-title">{alt}</span>
        <span className="card-meta">
          {item.site ?? "unknown site"}
          {item.extension ? ` · ${item.extension.toLowerCase()}` : ""}
        </span>
      </span>
    </Link>
  );
}
