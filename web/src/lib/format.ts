/**
 * Display formatters. Centralized so changes (e.g. switching currency
 * or relative-time style) happen in one place.
 */

/**
 * Money with full cent precision when small, dollar precision when large.
 * 0.034 -> "$0.034"
 * 12.45 -> "$12.45"
 * 1234  -> "$1,234"
 *
 * The build/audit costs we display are usually well under $1, so we
 * intentionally show more decimal places than a typical $X.XX format
 * would. Users care about the difference between $0.01 and $0.005.
 */
export function formatMoney(amount: number): string {
  if (!isFinite(amount)) return "—";
  if (amount === 0) return "$0";
  if (amount < 0.01) return `$${amount.toFixed(4)}`;
  if (amount < 1) return `$${amount.toFixed(3)}`;
  if (amount < 100) return `$${amount.toFixed(2)}`;
  return `$${Math.round(amount).toLocaleString()}`;
}

/**
 * Token counts: "1,234" or "1.2M" for large values.
 */
export function formatTokens(n: number): string {
  if (!isFinite(n)) return "—";
  if (n < 1000) return n.toString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/**
 * Relative time: "12s ago", "3m ago", "2h ago", "Apr 5".
 *
 * We avoid full timezone-aware date libraries; this is good enough
 * for the dashboard view. Falls back to a date on anything older
 * than a day.
 */
export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (isNaN(then)) return iso;
  const now = Date.now();
  const diffSec = Math.max(0, Math.round((now - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * Capitalize and humanize a slug-or-snake-case identifier.
 * "audit_project" -> "Audit project"
 */
export function humanize(s: string): string {
  if (!s) return "";
  const cleaned = s.replace(/[_-]+/g, " ");
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

/**
 * Provider display name. The backend uses "google" for Gemini calls
 * because that's the company; but in the UI users think of it as
 * "Gemini". Same for one or two others.
 */
export function providerLabel(provider: string): string {
  const map: Record<string, string> = {
    anthropic: "Claude",
    openai: "GPT",
    google: "Gemini",
  };
  return map[provider] ?? humanize(provider);
}
