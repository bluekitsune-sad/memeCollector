"use client";

export interface Feedback {
  kind: "ok" | "error";
  text: string;
}

interface DetailActionsProps {
  failed: boolean;
  busy: boolean;
  feedback: Feedback | null;
  onCopyFile: () => void;
  onCopyImage: () => void;
  onDelete: () => void;
  onReanalyze: () => void;
}

/** Copy / delete / reanalyze actions under the preview (PRD §26, §36). */
export default function DetailActions({
  failed,
  busy,
  feedback,
  onCopyFile,
  onCopyImage,
  onDelete,
  onReanalyze,
}: DetailActionsProps) {
  return (
    <>
      <div className="action-row" style={{ marginTop: 12 }}>
        <button type="button" className="btn" onClick={onCopyFile} disabled={busy}>
          Copy File
        </button>
        <button type="button" className="btn" onClick={onCopyImage} disabled={busy}>
          Copy Image
        </button>
        {failed ? (
          <button type="button" className="btn" onClick={onReanalyze} disabled={busy}>
            Reanalyze
          </button>
        ) : null}
        <button type="button" className="btn danger" onClick={onDelete} disabled={busy}>
          Delete
        </button>
      </div>

      {feedback ? (
        <p className={`inline-msg ${feedback.kind}`} role="status">
          {feedback.text}
        </p>
      ) : null}
    </>
  );
}
