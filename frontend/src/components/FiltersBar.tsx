import {
  DUP_OPTIONS,
  EMOTION_OPTIONS,
  FORMAT_OPTIONS,
  PROCESSING_GROUPS,
  TYPE_OPTIONS,
  hasActiveFilters,
  siteOptions,
} from "@/lib/filters";
import type { FilterValues } from "@/lib/filters";

interface FiltersBarProps {
  value: FilterValues;
  onChange: (next: FilterValues) => void;
  /** Sites seen on the current page, merged with the supported hosts. */
  observedSites: (string | null)[];
  /** Emotion only narrows `/api/search`, so the gallery hides the control. */
  showEmotion: boolean;
}

/** Filter bar for gallery + search (PRD §24) — values live in the URL. */
export default function FiltersBar({ value, onChange, observedSites, showEmotion }: FiltersBarProps) {
  function patch(next: Partial<FilterValues>): void {
    onChange({ ...value, ...next });
  }

  return (
    <section className="filters" aria-label="Filters">
      <div className="field">
        <label htmlFor="filter-type">Type</label>
        <select
          id="filter-type"
          className="select"
          value={value.type ?? ""}
          onChange={(event) => patch({ type: event.target.value })}
        >
          {TYPE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="filter-site">Source</label>
        <select
          id="filter-site"
          className="select"
          value={value.site ?? ""}
          onChange={(event) => patch({ site: event.target.value })}
        >
          <option value="">All sites</option>
          {siteOptions(observedSites).map((site) => (
            <option key={site} value={site}>
              {site}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="filter-format">Format</label>
        <select
          id="filter-format"
          className="select"
          value={value.format ?? ""}
          onChange={(event) => patch({ format: event.target.value })}
        >
          <option value="">All formats</option>
          {FORMAT_OPTIONS.map((format) => (
            <option key={format} value={format}>
              {format.toUpperCase()}
            </option>
          ))}
        </select>
      </div>

      {showEmotion ? (
        <div className="field">
          <label htmlFor="filter-emotion">Emotion</label>
          <select
            id="filter-emotion"
            className="select"
            value={value.emotion ?? ""}
            onChange={(event) => patch({ emotion: event.target.value })}
          >
            <option value="">Any emotion</option>
            {EMOTION_OPTIONS.map((emotion) => (
              <option key={emotion} value={emotion}>
                {emotion}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <div className="field">
        <label htmlFor="filter-chapter">Chapter</label>
        <input
          id="filter-chapter"
          className="input"
          type="text"
          placeholder="Any chapter"
          value={value.chapter ?? ""}
          onChange={(event) => patch({ chapter: event.target.value })}
        />
      </div>

      <div className="field">
        <label htmlFor="filter-date-from">Collected from</label>
        <input
          id="filter-date-from"
          className="input"
          type="date"
          value={value.date_from ?? ""}
          onChange={(event) => patch({ date_from: event.target.value })}
        />
      </div>

      <div className="field">
        <label htmlFor="filter-date-to">Collected to</label>
        <input
          id="filter-date-to"
          className="input"
          type="date"
          value={value.date_to ?? ""}
          onChange={(event) => patch({ date_to: event.target.value })}
        />
      </div>

      <div className="field">
        <label htmlFor="filter-status">AI status</label>
        <select
          id="filter-status"
          className="select"
          value={value.processing_status ?? ""}
          onChange={(event) => patch({ processing_status: event.target.value })}
        >
          {PROCESSING_GROUPS.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="filter-dup">Dup status</label>
        <select
          id="filter-dup"
          className="select"
          value={value.dup_status ?? ""}
          onChange={(event) => patch({ dup_status: event.target.value })}
        >
          {DUP_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="filters-actions">
        <button
          type="button"
          className="btn small ghost"
          onClick={() => onChange({})}
          disabled={!hasActiveFilters(value)}
        >
          Clear filters
        </button>
      </div>
    </section>
  );
}
