"use client";

import { useState } from "react";
import { mediaFileUrl } from "@/lib/api";
import { mediaKind, statusLabel } from "@/lib/format";
import type { MediaDetail } from "@/lib/types";

/** Large preview: original file, autoplaying GIF, or muted looping video (PRD §26). */
export default function MediaViewer({ item }: { item: MediaDetail }) {
  const [failed, setFailed] = useState(false);
  const src = mediaFileUrl(item.id, "file");
  const alt = item.title ?? item.original_filename ?? `Media ${item.id}`;
  const kind = mediaKind(item);

  if (failed) {
    return (
      <div className="detail-media">
        <p className="empty" style={{ border: "none" }}>
          Preview unavailable
          <br />
          <span className="inline-msg">
            {item.processing_status === "FAILED"
              ? `Processing status: ${statusLabel(item.processing_status)}`
              : "The original file could not be loaded."}
          </span>
        </p>
      </div>
    );
  }

  if (kind === "video") {
    return (
      <div className="detail-media">
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
      </div>
    );
  }

  return (
    <div className="detail-media">
      {/* Plain <img> on purpose: the original may be an animated GIF served from /api. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={src} alt={alt} className="pixel-art" onError={() => setFailed(true)} />
    </div>
  );
}
