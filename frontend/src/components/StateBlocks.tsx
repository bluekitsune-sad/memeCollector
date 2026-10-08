/** Shared loading / error / empty states used by every data-driven view. */

import type { ReactNode } from "react";

export function ErrorPanel({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-panel" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button className="btn small" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {hint ? <p>{hint}</p> : null}
      {action}
    </div>
  );
}

export function LoadingRow({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="loading-row" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </p>
  );
}

export function GridSkeleton({ count = 12 }: { count?: number }) {
  return (
    <div className="grid" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="skeleton card-skeleton" />
      ))}
    </div>
  );
}

export function DetailSkeleton() {
  return (
    <div className="detail" aria-hidden="true">
      <div className="skeleton" style={{ minHeight: "60vh" }} />
      <div className="skeleton" style={{ minHeight: "40vh" }} />
    </div>
  );
}

export function PanelSkeleton() {
  return <div className="skeleton" style={{ minHeight: "40vh" }} aria-hidden="true" />;
}
