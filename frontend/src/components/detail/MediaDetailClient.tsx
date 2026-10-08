"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import DetailActions, { type Feedback } from "./DetailActions";
import DetailInfo from "./DetailInfo";
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
import type { MediaUpdate } from "@/lib/types";

/** Full media page: preview + editable metadata + provenance (PRD §26–§28, §31). */
export default function MediaDetailClient({ id }: { id: number }) {
  const router = useRouter();
  const { data: item, error, loading, reload, mutate } = useAsync(() => getMedia(id), String(id));

  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const [descriptionDraft, setDescriptionDraft] = useState<string | null>(null);
  const [tagsDraft, setTagsDraft] = useState<string[] | null>(null);

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
    if (next === null || next === (item?.title ?? "")) return;
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
    if (!item) return;
    await apply({ is_favorite: !item.is_favorite }, item.is_favorite ? "Removed from favorites" : "Added to favorites");
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

  if (loading && !item) return <LoadingRow label="Loading media…" />;
  if (!item) {
    return <ErrorPanel message={error ?? "Media not found"} onRetry={reload} />;
  }

  return (
    <div className="detail">
      <div>
        <MediaViewer item={item} />
        <DetailActions
          failed={item.processing_status === "FAILED"}
          busy={busy}
          feedback={feedback}
          onCopyFile={() => void copyFile()}
          onCopyImage={() => void copyImage()}
          onDelete={() => void remove()}
          onReanalyze={() => void reanalyze()}
        />
      </div>

      <aside className="detail-side">
        <DetailInfo
          item={item}
          titleDraft={titleDraft ?? item.title ?? ""}
          descriptionDraft={descriptionDraft ?? item.user_description ?? item.ai_metadata?.description ?? ""}
          tagsDraft={tagsDraft}
          busy={busy}
          onTitleDraft={setTitleDraft}
          onTitleCommit={() => void commitTitle()}
          onDescriptionDraft={setDescriptionDraft}
          onSaveDescription={() => void saveDescription()}
          onTagsChange={(tags) => void changeTags(tags)}
          onToggleFavorite={() => void toggleFavorite()}
          onUnflag={() => void unflag()}
        />
        <SourceBlock sources={item.sources} collectedAt={item.created_at} />
      </aside>
    </div>
  );
}
