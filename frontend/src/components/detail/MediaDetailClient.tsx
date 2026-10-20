"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import DetailActions, { type Feedback } from "./DetailActions";
import DetailInfo from "./DetailInfo";
import DuplicateCopies from "./DuplicateCopies";
import MediaViewer from "./MediaViewer";
import SourceBlock from "./SourceBlock";
import { ErrorPanel, LoadingRow } from "@/components/StateBlocks";
import { copyImageAsPng, copyText, absoluteFileUrl } from "@/lib/clipboard";
import {
  deleteMedia,
  errorMessage,
  getMedia,
  mediaFileUrl,
  reanalyzeMedia,
  unflagDup,
  updateMedia,
} from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import { useMediaNeighbors } from "@/lib/neighbors";
import type { MediaDetail, MediaUpdate } from "@/lib/types";

/** Full media page: preview + editable metadata + provenance (PRD §26–§28, §31). */
export default function MediaDetailClient({ id }: { id: number }) {
  const router = useRouter();
  const { data: item, error, loading, reload, mutate } = useAsync(() => getMedia(id), String(id));

  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const [descriptionDraft, setDescriptionDraft] = useState<string | null>(null);
  const [tagsDraft, setTagsDraft] = useState<string[] | null>(null);

  // Keep the last loaded item so stepping to a neighbour shows the outgoing
  // image until the incoming one arrives instead of flashing a spinner.
  const [lastSeen, setLastSeen] = useState<MediaDetail | null>(null);
  useEffect(() => {
    if (item) setLastSeen(item);
  }, [item]);

  // A new id starts clean: drafts and feedback belong to the previous image.
  useEffect(() => {
    setBusy(false);
    setFeedback(null);
    setTitleDraft(null);
    setDescriptionDraft(null);
    setTagsDraft(null);
  }, [id]);

  const { canPrev, canNext, goPrev, goNext } = useMediaNeighbors(id);

  // ← / → step through the grid; fields and media controls keep their arrows.
  useEffect(() => {
    function handleKey(event: KeyboardEvent): void {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (
          tag === "INPUT" ||
          tag === "TEXTAREA" ||
          tag === "SELECT" ||
          tag === "VIDEO" ||
          target.isContentEditable
        ) {
          return;
        }
      }
      event.preventDefault();
      if (event.key === "ArrowLeft") {
        if (canPrev) void goPrev();
      } else if (canNext) {
        void goNext();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [canPrev, canNext, goPrev, goNext]);

  const shown = item ?? lastSeen;

  async function apply(update: MediaUpdate, success: string): Promise<boolean> {
    setBusy(true);
    setFeedback(null);
    try {
      mutate(await updateMedia(id, update));
      setFeedback({ kind: "ok", text: success });
      return true;
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function commitTitle(): Promise<void> {
    const next = titleDraft;
    setTitleDraft(null);
    if (next === null || next === (shown?.title ?? "")) return;
    await apply({ title: next }, "Title saved");
  }

  async function saveDescription(): Promise<void> {
    if (descriptionDraft === null) return;
    const saved = await apply({ user_description: descriptionDraft }, "Description saved");
    if (saved) setDescriptionDraft(null);
  }

  async function changeTags(next: string[]): Promise<void> {
    setTagsDraft(next);
    await apply({ user_tags: next }, "Tags saved");
    setTagsDraft(null);
  }

  async function toggleFavorite(): Promise<void> {
    if (!shown) return;
    await apply({ is_favorite: !shown.is_favorite }, shown.is_favorite ? "Removed from favorites" : "Added to favorites");
  }

  async function unflag(): Promise<void> {
    setBusy(true);
    setFeedback(null);
    try {
      mutate(await unflagDup(id));
      setFeedback({ kind: "ok", text: "Unflagged — this copy will no longer be auto-deleted." });
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function copyFile(): Promise<void> {
    setFeedback(null);
    try {
      await copyText(absoluteFileUrl(mediaFileUrl(id, "file")));
      setFeedback({ kind: "ok", text: "File URL copied to the clipboard." });
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function copyImage(): Promise<void> {
    setFeedback(null);
    try {
      await copyImageAsPng(mediaFileUrl(id, "file"));
      setFeedback({ kind: "ok", text: "Image copied to the clipboard." });
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function remove(): Promise<void> {
    if (!window.confirm("Delete this media and its files permanently?")) return;
    setBusy(true);
    setFeedback(null);
    try {
      await deleteMedia(id);
      router.push("/");
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
      setBusy(false);
    }
  }

  async function reanalyze(): Promise<void> {
    setBusy(true);
    setFeedback(null);
    try {
      await reanalyzeMedia(id);
      reload();
      setFeedback({ kind: "ok", text: "Reanalysis queued." });
    } catch (caught) {
      setFeedback({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  // A failed load of the current id wins over the stale image; otherwise the
  // outgoing item stays visible until the incoming one has arrived.
  if (item === null && error && !loading) return <ErrorPanel message={error} onRetry={reload} />;
  if (!shown) {
    if (loading) return <LoadingRow label="Loading media…" />;
    return <ErrorPanel message={error ?? "Media not found"} onRetry={reload} />;
  }
  const pending = item === null; // next/previous id still loading

  return (
    <div className="detail">
      <div>
        <MediaViewer
          key={shown.id}
          item={shown}
          nav={{
            canPrev,
            canNext,
            onPrev: () => void goPrev(),
            onNext: () => void goNext(),
          }}
        />
        <DuplicateCopies item={shown} />
        <DetailActions
          failed={shown.processing_status === "FAILED"}
          busy={busy || pending}
          feedback={feedback}
          onCopyFile={() => void copyFile()}
          onCopyImage={() => void copyImage()}
          onDelete={() => void remove()}
          onReanalyze={() => void reanalyze()}
        />
      </div>

      <aside className="detail-side">
        <DetailInfo
          item={shown}
          titleDraft={titleDraft ?? shown.title ?? ""}
          descriptionDraft={descriptionDraft ?? shown.user_description ?? shown.ai_metadata?.description ?? ""}
          tagsDraft={tagsDraft}
          busy={busy || pending}
          onTitleDraft={setTitleDraft}
          onTitleCommit={() => void commitTitle()}
          onDescriptionDraft={setDescriptionDraft}
          onSaveDescription={() => void saveDescription()}
          onTagsChange={(tags) => void changeTags(tags)}
          onToggleFavorite={() => void toggleFavorite()}
          onUnflag={() => void unflag()}
        />
        <SourceBlock sources={shown.sources} collectedAt={shown.created_at} />
      </aside>
    </div>
  );
}
