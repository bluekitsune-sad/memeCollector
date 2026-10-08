/** Determinate progress bar (values are 0–1 from the API; >1 treated as %). */
export function progressPercent(progress: number): number {
  const clamped = Math.min(100, Math.max(0, progress <= 1 ? progress * 100 : progress));
  return Math.round(clamped);
}

export default function ProgressBar({ progress, label = "Progress" }: { progress: number; label?: string }) {
  const percent = progressPercent(progress);
  return (
    <div
      className="progress"
      role="progressbar"
      aria-valuenow={percent}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <i style={{ width: `${percent}%` }} />
    </div>
  );
}
