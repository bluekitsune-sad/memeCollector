"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { getRandomMedia } from "@/lib/api";

const LINKS: { href: string; label: string }[] = [
  { href: "/", label: "Gallery" },
  { href: "/favorites", label: "Favorites" },
  { href: "/add", label: "Add Source" },
  { href: "/jobs", label: "Jobs" },
  { href: "/settings", label: "Settings" },
];

/** Top bar: brand, nav, keyword search, Random button (PRD §25, §30). */
export default function SiteHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [randomBusy, setRandomBusy] = useState(false);

  function onSearch(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const trimmed = query.trim();
    router.push(trimmed === "" ? "/search" : `/search?q=${encodeURIComponent(trimmed)}`);
  }

  async function onRandom(): Promise<void> {
    setRandomBusy(true);
    setNote(null);
    try {
      const item = await getRandomMedia("everything");
      router.push(`/media/${item.id}`);
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error));
    } finally {
      setRandomBusy(false);
    }
  }

  return (
    <header className="app-header">
      <Link href="/" className="brand" aria-label="MemeVault home">
        <span className="brand-mark" aria-hidden="true">
          M
        </span>
        MemeVault
      </Link>

      <nav className="nav" aria-label="Main">
        {LINKS.map((link) => {
          const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
          return (
            <Link key={link.href} href={link.href} className={`nav-link${active ? " active" : ""}`}>
              {link.label}
            </Link>
          );
        })}
      </nav>

      <div className="header-tools">
        <form className="header-search" onSubmit={onSearch} role="search">
          <label htmlFor="site-search" className="visually-hidden">
            Search memes
          </label>
          <input
            id="site-search"
            className="input"
            type="search"
            placeholder="Search memes…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="btn" type="submit">
            Search
          </button>
        </form>
        <button className="btn primary" onClick={() => void onRandom()} disabled={randomBusy}>
          {randomBusy ? <span className="spinner" aria-hidden="true" /> : null}
          Random
        </button>
      </div>

      {note ? (
        <p className="header-note" role="status">
          {note}
        </p>
      ) : null}
    </header>
  );
}
