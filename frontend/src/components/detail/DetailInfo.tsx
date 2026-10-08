"use client";

import DupBadge from "./DupBadge";
import TagEditor from "./TagEditor";
import { statusLabel, statusTone } from "@/lib/format";
import type { MediaDetail } from "@/lib/types";

interface DetailInfoProps {
  item: MediaDetail;
  titleDraft: string;
  descriptionDraft: string;
  tagsDraft: string[] | null;
  busy: boolean;
  onTitleDraft: (value: string) => void;
  onTitleCommit: () => void;
  onDescriptionDraft: (value: string) => void;
  onSaveDescription: () => void;
  onTagsChange: (tags: string[]) => void;
  onToggleFavorite: () => void;
  onUnflag: () => void;
}

/** Title, badges, description, tags and emotions panel (PRD §26, §27, §28). */
export default function DetailInfo({
  item,
  titleDraft,
  descriptionDraft,
  tagsDraft,
  busy,
  onTitleDraft,
  onTitleCommit,
  onDescriptionDraft,
  onSaveDescription,
  onTagsChange,
  onToggleFavorite,
  onUnflag,
}: DetailInfoProps) {
  const ai = item.ai_metadata;
  const tags = tagsDraft ?? item.user_tags;
  const description = descriptionDraft;
  const descriptionSource = item.user_description !== null ? "Your description" : "AI description";
  const hasDescription = description.trim() !== "";

  return (
    <section className="panel">
      <label className="visually-hidden" htmlFor="detail-title">
        Title
      </label>
      <input
        id="detail-title"
        className="detail-title"
        value={titleDraft}
        placeholder={item.original_filename ?? "Untitled"}
        onChange={(event) => onTitleDraft(event.target.value)}
        onBlur={onTitleCommit}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
        }}
      />

      <div className="badge-row" style={{ margin: "8px 0 12px" }}>
        <span className={`badge ${statusTone(item.processing_status)}`}>
          {statusLabel(item.processing_status)}
        </span>
        <DupBadge item={item} busy={busy} onUnflag={onUnflag} />
        <button
          type="button"
          className={`favorite-btn${item.is_favorite ? " on" : ""}`}
          aria-pressed={item.is_favorite}
          onClick={onToggleFavorite}
          disabled={busy}
        >
          {item.is_favorite ? "❤️ Favorited" : "🤍 Favorite"}
        </button>
      </div>

      <div className="field" style={{ marginBottom: 12 }}>
        <label htmlFor="detail-description">
          Description <span className="inline-msg">({hasDescription ? descriptionSource : "empty"})</span>
        </label>
        <textarea
          id="detail-description"
          className="textarea"
          value={description}
          placeholder={ai?.description ?? "No description yet"}
          onChange={(event) => onDescriptionDraft(event.target.value)}
        />
        <div className="action-row">
          <button
            type="button"
            className="btn small primary"
            onClick={onSaveDescription}
            disabled={busy}
          >
            Save description
          </button>
          <span className="inline-msg">
            {ai?.model ? `${ai.ai_provider ?? "AI"} · ${ai.model}` : "AI has not analyzed this item yet"}
          </span>
        </div>
      </div>

      <div className="field" style={{ marginBottom: 12 }}>
        <label htmlFor="detail-tags">Tags</label>
        <TagEditor tags={tags} onChange={onTagsChange} busy={busy} />
      </div>

      <div className="field">
        <span className="visually-hidden">Emotions</span>
        <h3>Emotions</h3>
        <div className="chip-row">
          {(ai?.emotions ?? []).length === 0 ? (
            <span className="inline-msg">None detected</span>
          ) : (
            (ai?.emotions ?? []).map((emotion) => (
              <span className="chip plain" key={emotion}>
                {emotion}
              </span>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
