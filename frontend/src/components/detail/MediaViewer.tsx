"use client";

import { useState, type ReactNode } from "react";
import { mediaFileUrl } from "@/lib/api";
import { mediaKind, statusLabel } from "@/lib/format";
import type { MediaDetail } from "@/lib/types";

/** Prev/next wiring from the detail page — the click zones at the viewer's edges. */
export interface ViewerNav {
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
}

/** Large preview: original file, autoplaying GIF, or muted looping video (PRD §26). */
export default function MediaViewer({ item, nav }: { item: MediaDetail; nav?: ViewerNav }) {
  const [failed, setFailed] = useState(false);
  const src = mediaFileUrl(item.id, "file");
  const alt = item.title ?? item.original_filename ?? `Media ${item.id}`;
  const kind = mediaKind(item);

  let media: ReactNode;
  if (failed) {
    media = (
      <p className="empty" style={{ border: "none" }}>
        Preview unavailable
        <br />
        <span className="inline-msg">
          {item.processing_status === "FAILED"
            ? `Processing status: ${statusLabel(item.processing_status)}`
            : "The original file could not be loaded."}
        </span>
      </p>
    );
  } else if (kind === "video") {
    media = (
      <video
        src={src}
        controls
        autoPlay
        muted
        loop
        playsInline
        onError={() => setFailed(true)}
        aria-label={alt}
      />
    );
  } else {
    media = (
      // Plain <img> on purpose: the original may be an animated GIF served from /api.
      // eslint-disable-next-line @next/next/no-img-element
      <img src={src} alt={alt} className="pixel-art" onError={() => setFailed(true)} />
    );
  }

  return (
    <div className="detail-media">
      {media}
      {nav ? (
        <>
          <button
            type="button"
            className="viewer-nav prev"
            onClick={nav.onPrev}
            disabled={!nav.canPrev}
            aria-label="Previous image"
            title="Previous image (←)"
          >
            ‹
          </button>
          <button
            type="button"
            className="viewer-nav next"
            onClick={nav.onNext}
            disabled={!nav.canNext}
            aria-label="Next image"
            title="Next image (→)"
          >
            ›
          </button>
        </>
      ) : null}
    </div>
  );
}
