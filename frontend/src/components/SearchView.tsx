"use client";

import FiltersBar from "./FiltersBar";
import GalleryGrid from "./GalleryGrid";
import Pagination from "./Pagination";
import { searchMedia } from "@/lib/api";
import { useFilterUrl } from "@/lib/filterUrl";
import { useAsync } from "@/lib/hooks";
import type { SearchMode, SearchWeights } from "@/lib/types";

/** Honesty about degraded search modes (missing embeddings, filters-only). */
const MODE_COPY: Record<SearchMode, { label: string; note: string }> = {
  hybrid: { label: "hybrid", note: "" },
  keyword_only: {
    label: "keyword only",
    note: "semantic index unavailable — results are ranked by keyword and tags",
  },
  filters_only: {
    label: "filters only",
    note: "no query text — showing items that match the filters",
  },
};

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function weightLine(weights: SearchWeights): string {
  return `semantic ${percent(weights.semantic)} / keyword ${percent(weights.keyword)} / tags ${percent(
    weights.tag,
  )} / metadata ${percent(weights.metadata)}`;
}

/** Search results: same grid as the gallery, driven by `/api/search` (PRD §23–§24). */
export default function SearchView() {
  const { filters, page, pageSize, q, urlKey, setFilters, setPage, setPageSize } = useFilterUrl();

  const { data, error, loading, reload } = useAsync(
    () => searchMedia({ ...filters, q, page, page_size: pageSize }),
    urlKey,
  );

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const mode = data?.mode ?? "hybrid";
  const copy = MODE_COPY[mode];

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">{q ? `“${q}”` : "Search"}</h1>
        <span className="page-sub">
          {loading && !data ? "Searching…" : `${total} result${total === 1 ? "" : "s"}`}
        </span>
      </div>

      {data ? (
        <p className="search-meta">
          <span className={`badge${mode === "hybrid" ? " ready" : " processing"}`}>{copy.label}</span>
          <span>{weightLine(data.weights)}</span>
          {copy.note ? <span className="inline-msg">— {copy.note}</span> : null}
        </p>
      ) : null}

      <FiltersBar
        value={filters}
        onChange={setFilters}
        observedSites={items.map((item) => item.site)}
        showEmotion
      />

      <GalleryGrid
        items={items}
        loading={loading}
        error={error}
        onRetry={reload}
        showScore
        emptyTitle={q ? `No matches for “${q}”` : "Nothing to show"}
        emptyHint={
          q
            ? "Try fewer words, a different emotion, or clear the filters."
            : "Type a query in the search box, or set filters below."
        }
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
        disabled={!data}
      />
    </>
  );
}
