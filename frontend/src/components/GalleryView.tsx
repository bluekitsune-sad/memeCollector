"use client";

import Link from "next/link";
import FiltersBar from "./FiltersBar";
import GalleryGrid from "./GalleryGrid";
import Pagination from "./Pagination";
import StatusStrip from "./StatusStrip";
import { listMedia } from "@/lib/api";
import { useFilterUrl } from "@/lib/filterUrl";
import { hasActiveFilters } from "@/lib/filters";
import { useAsync } from "@/lib/hooks";

interface GalleryViewProps {
  /** `favorites` pins is_favorite=1 (PRD §28). */
  mode: "library" | "favorites";
}

/** Filterable, paginated thumbnail gallery for `/` and `/favorites` (PRD §24, §25, §50). */
export default function GalleryView({ mode }: GalleryViewProps) {
  const { filters, page, pageSize, urlKey, setFilters, setPage, setPageSize } = useFilterUrl();
  const favorites = mode === "favorites";

  const { data, error, loading, reload } = useAsync(
    () =>
      listMedia({
        ...filters,
        ...(favorites ? { is_favorite: "1" } : {}),
        page,
        page_size: pageSize,
      }),
    `${mode}|${urlKey}`,
  );

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const filtered = hasActiveFilters(filters);

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">{favorites ? "Favorites" : "Library"}</h1>
        <span className="page-sub">
          {loading && !data ? "Loading…" : `${total} item${total === 1 ? "" : "s"}`}
        </span>
      </div>

      <StatusStrip
        total={total}
        statusCounts={data?.status_counts}
        dupCounts={data?.dup_counts}
        dupFilter={filters.dup_status ?? ""}
        onDupFilter={(value) =>
          setFilters({ ...filters, dup_status: value === "" ? undefined : value })
        }
      />

      <FiltersBar
        value={filters}
        onChange={setFilters}
        observedSites={items.map((item) => item.site)}
        showEmotion={false}
      />

      <GalleryGrid
        items={items}
        loading={loading}
        error={error}
        onRetry={reload}
        emptyTitle={
          favorites
            ? "No favorites yet"
            : filtered
              ? "No memes match these filters"
              : "No memes yet — add a source"
        }
        emptyHint={
          favorites
            ? "Tap the ❤ button on any meme to keep it here."
            : filtered
              ? "Try clearing a filter."
              : "Collect media from a supported comic site to start your archive."
        }
      />

      {!favorites && !filtered && items.length === 0 && !loading && !error ? (
        <p style={{ textAlign: "center" }}>
          <Link className="btn primary" href="/add">
            Add a source
          </Link>
        </p>
      ) : null}

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
