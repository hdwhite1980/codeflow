"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { FindingList } from "@/components/finding-list";
import { formatMoney, formatTokens, providerLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  AuditVerdict,
  Severity,
  UsageRow,
} from "@/lib/types";

interface FileListProps {
  /**
   * Paths of files in the order they were generated. Comes from the
   * artifacts response; we use it to keep file ordering stable.
   */
  filePaths: string[];
  /**
   * All usage rows for the project. We filter for stage=file and the
   * matching subject path to compute per-file build cost.
   */
  usageRows: UsageRow[];
  /**
   * All audit verdicts (from all auditors). Grouped by file_path.
   */
  verdicts: AuditVerdict[];
}

/**
 * Per-file expandable cards. Each card shows:
 *   - File path
 *   - Build cost + tokens
 *   - Per-auditor finding counts (with severity coloring)
 *
 * Clicking expands to show all findings from all auditors for that file.
 */
export function FileList({ filePaths, usageRows, verdicts }: FileListProps) {
  if (filePaths.length === 0) {
    return (
      <div className="text-sm italic text-muted-foreground">
        No files generated yet.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {filePaths.map((path) => (
        <FileRow
          key={path}
          path={path}
          buildRows={usageRows.filter(
            (r) => r.stage === "file" && r.subject === path,
          )}
          verdicts={verdicts.filter((v) => v.file_path === path)}
        />
      ))}
    </div>
  );
}

function FileRow({
  path,
  buildRows,
  verdicts,
}: {
  path: string;
  buildRows: UsageRow[];
  verdicts: AuditVerdict[];
}) {
  const [expanded, setExpanded] = useState(false);

  const buildCost = buildRows.reduce((s, r) => s + r.total_cost_usd, 0);
  const totalTokens = buildRows.reduce(
    (s, r) => s + r.input_tokens + r.output_tokens,
    0,
  );
  const auditCount = verdicts.length;
  const totalFindings = verdicts.reduce(
    (s, v) => s + v.findings.length,
    0,
  );

  return (
    <Card
      className={cn(
        "transition-colors",
        expanded && "ring-1 ring-border",
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="flex w-full items-center justify-between gap-4 p-4 text-left"
      >
        <div className="flex min-w-0 flex-1 items-center gap-3">
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <code className="truncate font-mono text-sm">{path}</code>
        </div>
        <div className="flex items-center gap-4">
          <FileSeveritySummary verdicts={verdicts} />
          <div className="hidden flex-col items-end text-xs text-muted-foreground sm:flex">
            <span className="tabular-nums">{formatMoney(buildCost)}</span>
            <span>{formatTokens(totalTokens)} tok</span>
          </div>
        </div>
      </button>
      {expanded && (
        <CardContent className="border-t border-border pt-4">
          {auditCount === 0 ? (
            <div className="text-sm italic text-muted-foreground">
              No audit verdicts yet.
            </div>
          ) : (
            <div className="grid gap-4 md:grid-cols-2">
              {verdicts.map((v) => (
                <div key={v.artifact_key} className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Badge variant="provider">
                      {providerLabel(v.auditor)}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {v.finding_count}{" "}
                      {v.finding_count === 1 ? "finding" : "findings"}
                    </span>
                  </div>
                  <FindingList
                    findings={v.findings}
                    parseError={v.parse_error}
                  />
                </div>
              ))}
            </div>
          )}
          <div className="mt-4 text-xs text-muted-foreground">
            Build cost: {formatMoney(buildCost)} ·{" "}
            {formatTokens(totalTokens)} tokens · {totalFindings} total
            findings
          </div>
        </CardContent>
      )}
    </Card>
  );
}

function FileSeveritySummary({ verdicts }: { verdicts: AuditVerdict[] }) {
  // Aggregate severities across all auditors for this file.
  const counts: Record<Severity, number> = {
    critical: 0,
    warning: 0,
    nit: 0,
  };
  for (const v of verdicts) {
    for (const f of v.findings) {
      counts[f.severity]++;
    }
  }
  const items: Array<[Severity, number]> = [
    ["critical", counts.critical],
    ["warning", counts.warning],
    ["nit", counts.nit],
  ];
  const visible = items.filter(([, n]) => n > 0);
  if (visible.length === 0 && verdicts.length > 0) {
    return (
      <Badge variant="outline" className="font-normal">
        clean
      </Badge>
    );
  }
  if (visible.length === 0) {
    return null;
  }
  return (
    <div className="flex gap-1">
      {visible.map(([sev, n]) => (
        <Badge key={sev} variant={sev} className="tabular-nums">
          {n}
        </Badge>
      ))}
    </div>
  );
}
