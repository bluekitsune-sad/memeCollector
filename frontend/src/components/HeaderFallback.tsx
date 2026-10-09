import PixelMark from "@/components/PixelMark";

/**
 * Server-rendered placeholder for the header while the client header (which
 * needs `usePathname`) streams in on dynamic routes (PRD §25 shell).
 */
export default function HeaderFallback() {
  return (
    <header className="app-header">
      <span className="brand">
        <span className="brand-mark" aria-hidden="true">
          <PixelMark />
        </span>
        MemeVault
      </span>
    </header>
  );
}
