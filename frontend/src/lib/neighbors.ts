"use client";

/**
 * Prev/next browsing for the media detail viewer (PRD §25 grid → §26 detail).
 *
 * A gallery/search card links to `/media/{id}?…the view's own query string…`,
 * so the detail page knows exactly which grid it was opened from. The
 * neighbours of the current item are then the surrounding cards of that grid
 * — page boundaries are crossed lazily (one extra request on click), and a
 * direct visit (no query) anchors on the first library page.
 */

import { useCallback, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { listMedia, searchMedia } from "./api";
import { readFilters, type FilterValues } from "./filters";
import { useAsync } from "./hooks";
import type { MediaQueryParams } from "./types";

/** Filters + query + page of the grid behind the detail page (page is 1-based). */
interface GridContext {
  filters: FilterValues;
  q: string;
  page: number;
  pageSize: number;
}

/** Minimal page slice — ids for ordering, `total` for "is there a next page?". */
interface Slice {
  items: { id: number }[];
  total: number;
}

function clampInt(raw: string | null, fallback: number, max: number): number {
  if (raw === null) return fallback;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return Math.min(max, Math.floor(parsed));
}

function loadSlice(context: GridContext): Promise<Slice> {
  const params: MediaQueryParams = {
    ...context.filters,
    page: context.page,
    page_size: context.pageSize,
  };
  return context.q === "" ? listMedia(params) : searchMedia({ ...params, q: context.q });
}

/** Id of the touching card on the neighbouring page (last of page−1 / first of page+1). */
async function crossEdge(context: GridContext, direction: -1 | 1): Promise<number | null> {
  const page = context.page + direction;
  if (page < 1) return null;
  const slice = await loadSlice({ ...context, page });
  if (slice.items.length === 0) return null;
  return direction < 0 ? slice.items[slice.items.length - 1].id : slice.items[0].id;
}

export interface MediaNeighbors {
  /** A previous card exists — in this page or in an earlier one. */
  canPrev: boolean;
  /** A next card exists — in this page or in a later one. */
  canNext: boolean;
  /** Move to the previous/next card of the grid; resolves `false` when there is none. */
  goPrev: () => Promise<boolean>;
  goNext: () => Promise<boolean>;
}

/**
 * Neighbours of `id` within the grid the detail page was opened from.
 * When the item is not part of that slice at all (stale link, direct visit),
 * both controls stay hidden and the viewer behaves exactly as before.
 */
export function useMediaNeighbors(id: number): MediaNeighbors {
  const router = useRouter();
  const searchParams = useSearchParams();
  const qs = searchParams.toString();

  const context = useMemo<GridContext>(() => {
    const params = new URLSearchParams(qs);
    return {
      filters: readFilters(params),
      q: params.get("q") ?? "",
      page: clampInt(params.get("page"), 1, 1_000_000),
      pageSize: clampInt(params.get("page_size"), 24, 200),
    };
  }, [qs]);

  // Keyed by the context, not the id: stepping through a page never refetches it.
  const slice = useAsync(() => loadSlice(context), JSON.stringify(context));
  const data = slice.data;

  const index = data ? data.items.findIndex((item) => item.id === id) : -1;
  const canPrev = data !== null && index >= 0 && (index > 0 || context.page > 1);
  const canNext =
    data !== null &&
    index >= 0 &&
    (index < data.items.length - 1 || context.page * context.pageSize < data.total);

  const navigate = useCallback(
    (nextId: number) => router.replace(`/media/${nextId}${qs === "" ? "" : `?${qs}`}`),
    [router, qs],
  );

  const goPrev = useCallback(async (): Promise<boolean> => {
    if (data === null || index < 0) return false;
    let target = index > 0 ? data.items[index - 1].id : null;
    if (target === null && context.page > 1) target = await crossEdge(context, -1);
    if (target === null) return false;
    navigate(target);
    return true;
  }, [data, index, context, navigate]);

  const goNext = useCallback(async (): Promise<boolean> => {
    if (data === null || index < 0) return false;
    let target = index < data.items.length - 1 ? data.items[index + 1].id : null;
    if (target === null && context.page * context.pageSize < data.total) {
      target = await crossEdge(context, 1);
    }
    if (target === null) return false;
    navigate(target);
    return true;
  }, [data, index, context, navigate]);

  return { canPrev, canNext, goPrev, goNext };
}
