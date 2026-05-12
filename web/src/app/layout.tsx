import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "Code Flow",
  description: "Multi-AI app builder with live audit verdicts.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // We hardcode `dark` on the html element rather than using a theme
  // provider — the app is dark-only and adding a provider just for
  // CSS-variable scoping would be overkill. If we ever add a light
  // toggle, swap this for a ThemeProvider that mounts a class on <html>.
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background text-foreground antialiased">
        <header className="border-b border-border">
          <div className="container flex h-14 items-center justify-between">
            <Link
              href="/"
              className="flex items-center gap-2 text-sm font-semibold"
            >
              <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
              Code Flow
            </Link>
            <nav className="text-xs text-muted-foreground">
              Multi-AI app builder
            </nav>
          </div>
        </header>
        <main className="container py-8">{children}</main>
      </body>
    </html>
  );
}
