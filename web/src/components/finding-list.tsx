import { Badge } from "@/components/ui/badge";
import type { Finding, Severity } from "@/lib/types";

interface FindingListProps {
  findings: Finding[];
  parseError?: boolean;
}

/**
 * Renders the list of findings produced by one auditor on one file.
 * Designed to be readable at a glance:
 *
 *   [CRITICAL] [security] L23
 *     The upload reads the entire file into memory with no size limit...
 *     → Add a max file size check or stream the upload to disk.
 *
 * Sort order: severity (critical → warning → nit) then by line number.
 * Stable, predictable, scannable.
 */
export function FindingList({ findings, parseError }: FindingListProps) {
  if (parseError) {
    return (
      <div className="rounded-md border border-amber-900/40 bg-amber-950/30 p-3 text-xs text-amber-300">
        Auditor returned an unparseable response. Verdict not produced.
      </div>
    );
  }
  if (findings.length === 0) {
    return (
      <div className="text-xs italic text-muted-foreground">
        No findings.
      </div>
    );
  }
  const sorted = [...findings].sort((a, b) => {
    const order = severityRank(a.severity) - severityRank(b.severity);
    if (order !== 0) return order;
    return (a.line ?? 0) - (b.line ?? 0);
  });
  return (
    <ul className="space-y-2">
      {sorted.map((f, i) => (
        <li
          key={i}
          className="rounded-md border border-border bg-background/40 p-3"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={f.severity}>{f.severity.toUpperCase()}</Badge>
            <Badge variant="outline" className="font-normal">
              {f.category}
            </Badge>
            {f.line !== null && (
              <span className="text-xs text-muted-foreground">
                line {f.line}
              </span>
            )}
          </div>
          <div className="mt-2 text-sm leading-snug">{f.issue}</div>
          {f.suggested_fix && (
            <div className="mt-1 text-xs text-muted-foreground">
              → {f.suggested_fix}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function severityRank(s: Severity): number {
  switch (s) {
    case "critical":
      return 0;
    case "warning":
      return 1;
    case "nit":
      return 2;
    default:
      return 3;
  }
}
