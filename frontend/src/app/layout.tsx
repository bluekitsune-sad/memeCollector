import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";
import HeaderFallback from "@/components/HeaderFallback";
import SiteHeader from "@/components/SiteHeader";
import "./globals.css";
import "./mistral.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "MemeVault",
    template: "%s · MemeVault",
  },
  description:
    "Local-first archive of memes collected from comic comment sections — searchable by keyword and meaning.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <Suspense fallback={<HeaderFallback />}>
          <SiteHeader />
        </Suspense>
        <main className="app-main">{children}</main>
      </body>
    </html>
  );
}
