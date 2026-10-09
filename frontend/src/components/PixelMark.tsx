/**
 * 16×16 pixel-art vault mark for MemeCollector (theme flourish).
 * `shape-rendering="crispEdges"` keeps every rect hard-edged at any size;
 * the brand box scales it ×2 (32px) so each art pixel lands on 2 screen px.
 */
export default function PixelMark() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="32"
      height="32"
      shapeRendering="crispEdges"
      aria-hidden="true"
      focusable="false"
    >
      {/* Frame (light ink on the dark palette) */}
      <rect width="16" height="16" fill="#f7f7f5" />
      {/* Dark body */}
      <rect x="1" y="1" width="14" height="14" fill="#171717" />
      {/* Vault door */}
      <rect x="3" y="3" width="10" height="10" fill="#f7f7f5" />
      {/* Door bolts */}
      <rect x="4" y="4" width="1" height="1" fill="#171717" />
      <rect x="11" y="4" width="1" height="1" fill="#171717" />
      <rect x="4" y="11" width="1" height="1" fill="#171717" />
      <rect x="11" y="11" width="1" height="1" fill="#171717" />
      {/* Orange combination wheel */}
      <rect x="6" y="6" width="4" height="4" fill="#ff4f00" />
      <rect x="7" y="4" width="2" height="8" fill="#ff4f00" />
      <rect x="4" y="7" width="8" height="2" fill="#ff4f00" />
      {/* Keyhole hub */}
      <rect x="7" y="7" width="2" height="2" fill="#171717" />
    </svg>
  );
}
