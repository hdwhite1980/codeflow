"use client";

import { useState } from "react";
import { Wand2, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getFixAllEstimate, triggerFixAll } from "@/lib/api";
import type { FixAllEstimate } from "@/lib/types";

interface FixAllButtonProps {
  projectId: string;
  onTriggered: (seq: number) => void;
  /** True if a fix-all pass is already running for this project. */
  inFlight: boolean;
}

/**
 * "Fix all issues" button + confirmation modal.
 *
 * Click → fetch estimate (count + cost range) → open modal showing
 * what'll happen → confirm → POST /fix-all. The modal exists because
 * fix-all isn't free — the user should see what they're committing to
 * before they spend $1-2.
 *
 * Per Hugh's spec: one pass per click. The user gets a clear report
 * afterward; if there's still work to do, they can click again.
 */
export function FixAllButton({
  projectId, onTriggered, inFlight,
}: FixAllButtonProps) {
  const [estimateLoading, setEstimateLoading] = useState(false);
  const [estimate, setEstimate] = useState<FixAllEstimate | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function openModal() {
    setError(null);
    setEstimateLoading(true);
    try {
      const est = await getFixAllEstimate(projectId);
      setEstimate(est);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load estimate",
      );
    } finally {
      setEstimateLoading(false);
    }
  }

  function closeModal() {
    setEstimate(null);
    setError(null);
  }

  async function confirm() {
    if (!estimate) return;
    setSubmitting(true);
    setError(null);
    try {
      const resp = await triggerFixAll(projectId);
      onTriggered(resp.fix_all_seq);
      closeModal();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to start fix-all",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void openModal()}
        disabled={estimateLoading || inFlight}
        className="inline-flex items-center gap-1 rounded-md border border-violet-700/50 bg-violet-950/30 px-2.5 py-1 text-xs text-violet-300 transition-colors hover:border-violet-600 hover:text-violet-100 disabled:opacity-60"
        title="Have Code Flow attempt to fix every issue the auditors flagged"
      >
        {estimateLoading ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : inFlight ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <Wand2 className="h-3 w-3" />
        )}
        {inFlight ? "Fixing…" : "Fix all issues"}
      </button>

      {estimate && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={closeModal}
        >
          <div
            className="w-full max-w-md rounded-lg border border-border bg-card p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold">Fix all issues?</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Code Flow will read every audit finding and try to fix them
              in one pass. Any issue it can&apos;t safely fix will be
              listed in the report with an explanation.
            </p>

            {estimate.issues_to_fix === 0 ? (
              <div className="mt-4 rounded-md border border-emerald-900/40 bg-emerald-950/20 px-3 py-2 text-sm text-emerald-300">
                No issues to fix — the audit is already clean.
              </div>
            ) : (
              <>
                <div className="mt-4 space-y-2 rounded-md border border-border bg-background/30 px-3 py-2 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Issues</span>
                    <span className="tabular-nums font-medium">
                      {estimate.issues_to_fix}
                      {estimate.truncated && " (capped)"}
                    </span>
                  </div>
                  {estimate.by_severity && (
                    <div className="flex flex-wrap gap-1.5">
                      {estimate.by_severity.critical > 0 && (
                        <Badge variant="critical" className="text-[10px]">
                          {estimate.by_severity.critical} critical
                        </Badge>
                      )}
                      {estimate.by_severity.warning > 0 && (
                        <Badge variant="warning" className="text-[10px]">
                          {estimate.by_severity.warning} warning
                        </Badge>
                      )}
                      {estimate.by_severity.nit > 0 && (
                        <Badge variant="nit" className="text-[10px]">
                          {estimate.by_severity.nit} nit
                        </Badge>
                      )}
                    </div>
                  )}
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Files affected</span>
                    <span className="tabular-nums">
                      {estimate.files_affected}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">
                      Estimated cost
                    </span>
                    <span className="tabular-nums">
                      ${estimate.estimated_cost_usd_low.toFixed(2)}
                      {" – "}${estimate.estimated_cost_usd_high.toFixed(2)}
                    </span>
                  </div>
                </div>

                <p className="mt-3 text-[10px] text-muted-foreground">
                  This runs one fix pass + re-audit + report. Typical
                  duration: 1-3 minutes.
                </p>
              </>
            )}

            {error && (
              <div className="mt-3 rounded-md border border-red-900/40 bg-red-950/30 px-3 py-2 text-xs text-red-300">
                {error}
              </div>
            )}

            <div className="mt-5 flex items-center justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={closeModal}
                disabled={submitting}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => void confirm()}
                disabled={submitting || estimate.issues_to_fix === 0}
              >
                {submitting ? (
                  <>
                    <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                    Starting…
                  </>
                ) : (
                  "Fix all issues"
                )}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
