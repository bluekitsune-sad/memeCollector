"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { readFilters, filtersToQuery, type FilterValues } from "./filters";

export interface FilterUrlState {
  filters: FilterValues;
  page: number;
  pageSize: number;
  q: string;
  /** Stable string for the whole URL — use as an effect dependency. */
  urlKey: string;
}

export interface FilterUrlControls {
  /** Replaces the filters and rewinds to page 1. */
  setFilters: (filters: FilterValues) => void;
  setPage: (page: number) => void;
  setPageSize: (size: number) => void;
  /** Replaces the search text (search view) and rewinds to page 1. */
  setQuery: (q: string) => void;
}

function clamp(raw: string | null, fallback: number, min: number, max: number): number {
  if (raw === null) return fallback;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < min) return fallback;
  return Math.min(max, Math.floor(parsed));
}

/**
 * Filters/pagination/search state kept in the URL so every view is linkable
 * and the API only ever receives one page (PRD §50).
 */
export function useFilterUrl(defaultPageSize = 24): FilterUrlState & FilterUrlControls {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const urlKey = searchParams.toString();
  const filters = readFilters(searchParams);
  const page = clamp(searchParams.get("page"), 1, 1, 1_000_000);
  const pageSize = clamp(searchParams.get("page_size"), defaultPageSize, 1, 200);
  const q = searchParams.get("q") ?? "";

  function push(next: { filters?: FilterValues; page?: number; page_size?: number; q?: string }): void {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filtersToQuery(next.filters ?? filters))) {
      if (value) params.set(key, value);
    }
    const nextQuery = next.q ?? q;
    if (nextQuery !== "") params.set("q", nextQuery);
    params.set("page", String(next.page ?? 1));
    params.set("page_size", String(next.page_size ?? pageSize));
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  return {
    filters,
    page,
    pageSize,
    q,
    urlKey,
    setFilters: (next) => push({ filters: next, page: 1 }),
    setPage: (next) => push({ page: next }),
    setPageSize: (next) => push({ page: 1, page_size: next }),
    setQuery: (next) => push({ q: next, page: 1 }),
  };
}
