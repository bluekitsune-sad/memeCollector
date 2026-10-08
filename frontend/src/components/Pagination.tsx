import { formatCount } from "@/lib/format";

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  disabled?: boolean;
}

const PAGE_SIZES = [24, 50, 100, 200];

/** Page/page-size controls — the gallery never loads everything at once (PRD §50). */
export default function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  disabled = false,
}: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const firstItem = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const lastItem = Math.min(total, page * pageSize);

  return (
    <nav className="pagination" aria-label="Pagination">
      <button
        type="button"
        className="btn small"
        onClick={() => onPageChange(page - 1)}
        disabled={disabled || page <= 1}
      >
        ← Prev
      </button>
      <span>
        Page {formatCount(page)} of {formatCount(totalPages)} · {formatCount(firstItem)}–
        {formatCount(lastItem)} of {formatCount(total)}
      </span>
      <label className="visually-hidden" htmlFor="page-size">
        Items per page
      </label>
      <select
        id="page-size"
        className="select"
        value={pageSize}
        onChange={(event) => onPageSizeChange(Number(event.target.value))}
        disabled={disabled}
      >
        {PAGE_SIZES.map((size) => (
          <option key={size} value={size}>
            {size} / page
          </option>
        ))}
      </select>
      <button
        type="button"
        className="btn small"
        onClick={() => onPageChange(page + 1)}
        disabled={disabled || page >= totalPages}
      >
        Next →
      </button>
    </nav>
  );
}
