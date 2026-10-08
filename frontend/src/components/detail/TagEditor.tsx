"use client";

import { FormEvent, useState } from "react";

interface TagEditorProps {
  tags: string[];
  onChange: (next: string[]) => void;
  busy?: boolean;
}

/** Chip list with an add input — manual tags override AI tags (PRD §27). */
export default function TagEditor({ tags, onChange, busy = false }: TagEditorProps) {
  const [draft, setDraft] = useState("");

  function addTag(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const tag = draft.trim().replace(/^#+/, "");
    if (tag === "") return;
    if (!tags.some((existing) => existing.toLowerCase() === tag.toLowerCase())) {
      onChange([...tags, tag]);
    }
    setDraft("");
  }

  return (
    <div>
      <div className="chip-row">
        {tags.length === 0 ? <span className="inline-msg">No tags yet</span> : null}
        {tags.map((tag) => (
          <span className="chip" key={tag}>
            #{tag}
            <button
              type="button"
              aria-label={`Remove tag ${tag}`}
              onClick={() => onChange(tags.filter((existing) => existing !== tag))}
              disabled={busy}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <form className="action-row" onSubmit={addTag}>
        <label className="visually-hidden" htmlFor="new-tag">
          Add a tag
        </label>
        <input
          id="new-tag"
          className="input"
          type="text"
          placeholder="Add a tag…"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button className="btn small" type="submit" disabled={busy || draft.trim() === ""}>
          Add
        </button>
      </form>
    </div>
  );
}
