"use client";

import { useState } from "react";
import { Shield, ShieldAlert, Loader2, ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { askRisk } from "@/lib/api";
import type { RiskAssessment, RiskSeverity } from "@/lib/types";

interface RiskAnalyzerProps {
  projectId: string;
  /**
   * Optional initial target path. If the user clicked a file node and
   * then opened the risk panel, we can pre-fill the target so they
   * only have to type the change description.
   */
  initialTarget?: string;
  /**
   * Called with the assessment after a successful run. The parent can
   * use this to push into a history panel or trigger a refetch of the
   * /risks endpoint.
   */
  onAssessment?: (assessment: RiskAssessment) => void;
}

/**
 * "Ask the guardian: what breaks if I…" panel.
 *
 * Two-field form (target + change description) and a result panel
 * with the plain-English narrative shown by default, technical detail
 * expandable, and concerns/sequencing rendered as structured chips.
 *
 * The submit call blocks for the duration of the LLM analysis — up
 * to 2 minutes on local Ollama. We show a loading spinner with an
 * explanation of expected latency so users don't think it's hung.
 *
 * Per Hugh's spec: plain-English first, technical detail on click.
 * The severity badge and confidence score are always visible because
 * they're the first thing customers need to know.
 */
export function RiskAnalyzer({
  projectId, initialTarget, onAssessment,
}: RiskAnalyzerProps) {
  const [target, setTarget] = useState(initialTarget ?? "");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [assessment, setAssessment] = useState<RiskAssessment | null>(null);
  const [techExpanded, setTechExpanded] = useState(false);

  async function submit() {
    setError(null);
    setAssessment(null);
    if (!target.trim() || description.trim().length < 5) {
      setError(
        "Target and a change description of 5+ characters are both required.",
      );
      return;
    }
    setSubmitting(true);
    try {
      const result = await askRisk(projectId, {
        target: target.trim(),
        change_description: description.trim(),
      });
      setAssessment(result);
      if (onAssessment) onAssessment(result);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Risk analysis failed",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="rounded-md border border-border bg-card/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Shield className="h-4 w-4 text-violet-400" />
        <h3 className="text-sm font-semibold tracking-tight">
          Guardian: what breaks if I…
        </h3>
      </div>

      <div className="space-y-2">
        <div>
          <label className="block text-xs text-muted-foreground mb-1">
            Target (file path or artifact)
          </label>
          <Input
            type="text"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="app/auth.py"
            disabled={submitting}
            maxLength={500}
          />
        </div>
        <div>
          <label className="block text-xs text-muted-foreground mb-1">
            Describe the change
          </label>
          <Textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="rename verify_password to check_password"
            disabled={submitting}
            maxLength={2000}
            rows={3}
          />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            {submitting
              ? "Guardian is reasoning… can take 30s–2min on local model."
              : description.length > 0
                ? `${description.length}/2000`
                : ""}
          </span>
          <Button
            onClick={submit}
            disabled={submitting || !target.trim() || description.trim().length < 5}
            size="sm"
          >
            {submitting
              ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> Analyzing</>
              : "Ask the guardian"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {assessment && (
        <RiskResult
          assessment={assessment}
          techExpanded={techExpanded}
          onToggleTech={() => setTechExpanded(!techExpanded)}
        />
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Result panel — pure presentational, exported for use in history view.
// ---------------------------------------------------------------------------

interface RiskResultProps {
  assessment: RiskAssessment;
  techExpanded: boolean;
  onToggleTech: () => void;
}

export function RiskResult({
  assessment, techExpanded, onToggleTech,
}: RiskResultProps) {
  return (
    <div className="space-y-3 border-t border-border pt-3">
      <div className="flex items-center gap-3 flex-wrap">
        <SeverityBadge severity={assessment.severity} />
        <ConfidenceIndicator confidence={assessment.confidence} />
        <span className="text-xs text-muted-foreground">
          {assessment.affected_paths.length} affected ·{" "}
          {assessment.indexed_summary_count} indexed
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          {assessment.analyzer_model}
        </span>
      </div>

      {/* Plain-English narrative — shown by default per Hugh's spec. */}
      <div className="text-sm leading-relaxed">
        {assessment.plain_narrative || "(No plain-English summary produced.)"}
      </div>

      {/* Concerns — itemized, clickable chips. */}
      {assessment.concerns.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Specific concerns
          </div>
          {assessment.concerns.map((c, i) => (
            <div
              key={`${c.path}-${i}`}
              className="flex items-start gap-2 text-sm rounded border border-border bg-background/40 p-2"
            >
              <SeverityBadge severity={c.severity} small />
              <div className="min-w-0 flex-1">
                <div className="font-mono text-xs text-muted-foreground truncate">
                  {c.path}
                </div>
                <div className="text-sm mt-0.5">{c.reason}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Suggested sequencing — only render if non-empty. */}
      {assessment.suggested_sequencing.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Suggested sequencing
          </div>
          <ol className="text-sm space-y-1 list-decimal list-inside">
            {assessment.suggested_sequencing.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        </div>
      )}

      {/* Technical detail — collapsed by default; one click expands. */}
      <button
        onClick={onToggleTech}
        className="flex items-center gap-1 text-xs text-violet-400 hover:text-violet-300"
        type="button"
      >
        {techExpanded
          ? <ChevronDown className="h-3 w-3" />
          : <ChevronRight className="h-3 w-3" />}
        {techExpanded ? "Hide" : "Show"} technical detail
      </button>
      {techExpanded && (
        <div className="text-sm leading-relaxed border-l-2 border-violet-900/40 pl-3 text-muted-foreground">
          {assessment.technical_narrative ||
            "(No technical narrative produced.)"}
        </div>
      )}

      {/* Affected files chip list — last, since the concerns list usually
          covers the same ground but with reasons. */}
      {assessment.affected_paths.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Affected files
          </div>
          <div className="flex flex-wrap gap-1.5">
            {assessment.affected_paths.slice(0, 30).map((p) => (
              <code
                key={p}
                className="text-xs px-2 py-0.5 rounded bg-background/60 border border-border font-mono"
              >
                {p}
              </code>
            ))}
            {assessment.affected_paths.length > 30 && (
              <span className="text-xs text-muted-foreground">
                +{assessment.affected_paths.length - 30} more
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Small helper components.
// ---------------------------------------------------------------------------

function SeverityBadge({
  severity, small = false,
}: { severity: RiskSeverity; small?: boolean }) {
  const config: Record<RiskSeverity, { label: string; cls: string }> = {
    low: {
      label: "Low risk",
      cls: "bg-emerald-950/40 border-emerald-700/40 text-emerald-300",
    },
    medium: {
      label: "Medium risk",
      cls: "bg-amber-950/40 border-amber-700/40 text-amber-300",
    },
    high: {
      label: "High risk",
      cls: "bg-orange-950/40 border-orange-700/40 text-orange-300",
    },
    critical: {
      label: "Critical risk",
      cls: "bg-red-950/40 border-red-700/40 text-red-300",
    },
  };
  const c = config[severity];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-2 ${
        small ? "py-0 text-[10px]" : "py-0.5 text-xs"
      } font-medium ${c.cls}`}
    >
      <ShieldAlert className={small ? "h-2.5 w-2.5" : "h-3 w-3"} />
      {c.label}
    </span>
  );
}

function ConfidenceIndicator({ confidence }: { confidence: number }) {
  // Render confidence as a small bar + percentage. Anything below 0.5
  // gets a "low confidence" prefix to remind users the model said so.
  const pct = Math.round(confidence * 100);
  const colorClass =
    confidence >= 0.75 ? "bg-emerald-500"
      : confidence >= 0.5 ? "bg-amber-500"
        : "bg-red-500";
  return (
    <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
      <span className="relative w-12 h-1.5 rounded-full bg-border overflow-hidden">
        <span
          className={`absolute inset-y-0 left-0 ${colorClass}`}
          style={{ width: `${pct}%` }}
        />
      </span>
      {confidence < 0.5 && <span className="text-amber-400">low</span>}
      <span>{pct}% confidence</span>
    </span>
  );
}
