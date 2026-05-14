"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Brain,
  ChevronDown,
  ChevronRight,
  Code2,
  HelpCircle,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getProjectMemory, getProjectMemorySymbols, reindexFile,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ProjectMemoryEntry, SymbolSummary } from "@/lib/types";

interface ProjectMemoryPanelProps {
  projectId: string;
  /**
   * Bumped by the parent when guardian work might have produced new
   * summaries. The panel refetches when this changes.
   */
  refreshKey?: number;
}

/**
 * Project Memory panel — the literal "Project Memory" UI surface
 * Hugh asked for.
 *
 * Renders a searchable list of every guardian-indexed file in the
 * project, with each entry expandable to show the full semantic
 * summary plus cross-references to any risk queries that mentioned
 * the file. This is the user-visible answer to "Claude doesn't
 * remember my project" — the memory is right there, browsable.
 *
 * Empty state distinguishes three cases:
 *   - Project never indexed → "Index this project to populate memory"
 *   - Indexing in progress (some summaries exist, more queued) →
 *     "X files indexed, indexing continuing in the background"
 *   - No matches for current search → "No files match '<query>'"
 *
 * Re-index per file is a single button; the full project re-index
 * lives elsewhere (top of the page, near the guardian status).
 */
export function ProjectMemoryPanel({
  projectId, refreshKey = 0,
}: ProjectMemoryPanelProps) {
  const [summaries, setSummaries] = useState<ProjectMemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [reindexing, setReindexing] = useState<Set<string>>(new Set());

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getProjectMemory(projectId);
      setSummaries(resp.summaries);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load project memory",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    refetch();
  }, [refetch, refreshKey]);

  // Client-side filter. Cheap — projects rarely have 1000+ files.
  // We match against file_path, purpose, plain_english, and the
  // string-joined touches/assumes/risk_notes so the search is broad.
  const filtered = useMemo(() => {
    if (!query.trim()) return summaries;
    const q = query.toLowerCase();
    return summaries.filter((s) => {
      const blob =
        s.file_path + " " +
        s.purpose + " " +
        s.plain_english + " " +
        (s.touches || []).join(" ") + " " +
        (s.assumes || []).join(" ") + " " +
        (s.risk_notes || []).join(" ");
      return blob.toLowerCase().includes(q);
    });
  }, [summaries, query]);

  function toggleExpanded(path: string) {
    const next = new Set(expanded);
    if (next.has(path)) next.delete(path); else next.add(path);
    setExpanded(next);
  }

  async function handleReindex(path: string) {
    const next = new Set(reindexing);
    next.add(path);
    setReindexing(next);
    try {
      await reindexFile(projectId, path);
      // The worker will re-index in the background; we don't refetch
      // immediately because the new summary won't be ready yet. The
      // parent's WebSocket-driven refresh will pick it up when the
      // ledger entry lands.
    } catch (err) {
      console.error("[memory] reindex failed:", err);
    } finally {
      const cleared = new Set(reindexing);
      cleared.delete(path);
      setReindexing(cleared);
    }
  }

  return (
    <div className="rounded-md border border-border bg-card/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Brain className="h-4 w-4 text-violet-400" />
        <h3 className="text-sm font-semibold tracking-tight">
          Project Memory
        </h3>
        <span className="text-xs text-muted-foreground tabular-nums">
          {summaries.length} file{summaries.length === 1 ? "" : "s"}
        </span>
        <a
          href="/about-the-guardian"
          target="_blank"
          rel="noreferrer"
          title="What is the guardian?"
          className="ml-auto text-muted-foreground hover:text-violet-300"
        >
          <HelpCircle className="h-3 w-3" />
        </a>
        <Button
          variant="ghost"
          size="sm"
          onClick={refetch}
          disabled={loading}
          className="h-6 w-6 p-0"
          title="Refresh"
        >
          <RefreshCw className={cn("h-3 w-3", loading && "animate-spin")} />
        </Button>
      </div>

      {/* Search box. Filters live as the user types — no submit needed. */}
      {summaries.length > 0 && (
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
          <Input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search files, purpose, risks…"
            className="pl-8 h-8 text-xs"
          />
        </div>
      )}

      {error && (
        <div className="rounded border border-red-900/40 bg-red-950/30 p-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {loading && summaries.length === 0 && (
        <div className="text-xs text-muted-foreground">
          Loading project memory…
        </div>
      )}

      {!loading && summaries.length === 0 && !error && (
        <div className="text-xs text-muted-foreground leading-relaxed">
          The guardian hasn't indexed this project yet. Once it runs,
          you'll see every file here with its purpose, what it touches,
          and any risks flagged during indexing.
        </div>
      )}

      {summaries.length > 0 && filtered.length === 0 && (
        <div className="text-xs text-muted-foreground">
          No files match &quot;{query}&quot;.
        </div>
      )}

      {filtered.length > 0 && (
        <div className="space-y-1.5 max-h-[50vh] overflow-y-auto">
          {filtered.map((entry) => (
            <MemoryEntryRow
              key={entry.file_path}
              projectId={projectId}
              refreshKey={refreshKey}
              entry={entry}
              expanded={expanded.has(entry.file_path)}
              onToggleExpanded={() => toggleExpanded(entry.file_path)}
              reindexing={reindexing.has(entry.file_path)}
              onReindex={() => handleReindex(entry.file_path)}
            />
          ))}
        </div>
      )}
    </div>
  );
}


function MemoryEntryRow({
  projectId, refreshKey, entry, expanded, onToggleExpanded, reindexing, onReindex,
}: {
  projectId: string;
  refreshKey: number;
  entry: ProjectMemoryEntry;
  expanded: boolean;
  onToggleExpanded: () => void;
  reindexing: boolean;
  onReindex: () => void;
}) {
  const [showTech, setShowTech] = useState(false);

  // Symbol drill-down (Turn B frontend). Lazily fetched the first
  // time the user expands the file row and toggles "Symbols". Result
  // is cached for the lifetime of the row component so re-toggling
  // doesn't re-fetch. Refetched when refreshKey bumps (e.g. WS event
  // says new symbol summaries landed in the ledger).
  const [showSymbols, setShowSymbols] = useState(false);
  const [symbols, setSymbols] = useState<SymbolSummary[] | null>(null);
  const [symbolsLoading, setSymbolsLoading] = useState(false);
  const [symbolsError, setSymbolsError] = useState<string | null>(null);

  // Invalidate the symbol cache when the global refreshKey changes
  // AND the user has the symbols panel open. If they don't have it
  // open we don't waste an HTTP roundtrip — they'll fetch fresh
  // whenever they next expand it.
  useEffect(() => {
    if (refreshKey === 0) return;  // initial mount, don't refetch
    if (!showSymbols) {
      // Clear stale cache silently so next open fetches fresh.
      setSymbols(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const resp = await getProjectMemorySymbols(projectId, entry.file_path);
        if (!cancelled) setSymbols(resp.symbols);
      } catch (err) {
        if (!cancelled) {
          setSymbolsError(
            err instanceof Error ? err.message : "Failed to refresh symbols",
          );
        }
      }
    })();
    return () => { cancelled = true; };
  }, [refreshKey, projectId, entry.file_path, showSymbols]);

  async function fetchSymbolsIfNeeded() {
    if (symbols !== null || symbolsLoading) return;
    setSymbolsLoading(true);
    setSymbolsError(null);
    try {
      const resp = await getProjectMemorySymbols(projectId, entry.file_path);
      setSymbols(resp.symbols);
    } catch (err) {
      setSymbolsError(
        err instanceof Error ? err.message : "Failed to load symbols",
      );
    } finally {
      setSymbolsLoading(false);
    }
  }

  async function handleToggleSymbols() {
    const next = !showSymbols;
    setShowSymbols(next);
    if (next) await fetchSymbolsIfNeeded();
  }

  // Risk badge — visible at-a-glance count if the file was flagged
  // anywhere. We pool both target and concern queries.
  const totalRiskMentions =
    entry.risk_queries_as_target.length +
    entry.risk_queries_as_concern.length;

  return (
    <div className="rounded border border-border bg-background/40">
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={onToggleExpanded}
          className="flex flex-1 items-start gap-2 px-3 py-2 text-left min-w-0"
        >
          {expanded ? (
            <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <code className="text-xs font-medium break-all">
                {entry.file_path}
              </code>
              {totalRiskMentions > 0 && (
                <span
                  className="text-[9px] px-1 rounded bg-amber-950/40 border border-amber-700/40 text-amber-300"
                  title={`Referenced in ${totalRiskMentions} risk query/queries`}
                >
                  {totalRiskMentions} risk
                </span>
              )}
              {entry.is_stale && (
                <span
                  className="text-[9px] px-1 rounded bg-orange-950/40 border border-orange-700/40 text-orange-300"
                  title={
                    "This file has been written to the ledger after this " +
                    "summary was produced. The summary may not reflect the " +
                    "current contents. Click the re-index button to refresh."
                  }
                >
                  stale
                </span>
              )}
            </div>
            <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
              {entry.purpose || entry.plain_english || "(no summary)"}
            </div>
          </div>
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onReindex();
          }}
          disabled={reindexing}
          title={reindexing ? "Re-index queued" : "Re-index this file"}
          className="flex items-center justify-center px-2 border-l border-border text-muted-foreground hover:text-violet-300 hover:bg-violet-950/30 disabled:opacity-50"
        >
          {reindexing ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
        </button>
      </div>

      {expanded && (
        <div className="border-t border-border px-3 py-2 text-xs space-y-2">
          {/* Plain-English summary — primary content. */}
          {entry.plain_english && (
            <div>
              <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
                Summary
              </div>
              <div className="mt-0.5 leading-relaxed">
                {entry.plain_english}
              </div>
            </div>
          )}

          {/* What this file touches. Useful for "where is auth handled?" */}
          {entry.touches && entry.touches.length > 0 && (
            <LabeledChips label="Touches" items={entry.touches} />
          )}

          {/* Assumptions — invariants the file relies on. */}
          {entry.assumes && entry.assumes.length > 0 && (
            <LabeledChips label="Assumes" items={entry.assumes} />
          )}

          {/* Failure modes — how it can break. */}
          {entry.failure_modes && entry.failure_modes.length > 0 && (
            <LabeledChips
              label="Failure modes"
              items={entry.failure_modes}
              chipClass="text-amber-200 border-amber-900/40 bg-amber-950/20"
            />
          )}

          {/* Risk notes — most prominent, that's the "things a reviewer
              would flag" content. */}
          {entry.risk_notes && entry.risk_notes.length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-wide text-amber-400">
                Risk notes
              </div>
              <ul className="mt-0.5 space-y-0.5 list-disc list-inside text-[11px] text-amber-200">
                {entry.risk_notes.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Risk-query cross-references — surfaces which prior risk
              queries touched this file. */}
          {(entry.risk_queries_as_target.length > 0 ||
            entry.risk_queries_as_concern.length > 0) && (
            <div>
              <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
                Risk queries about this file
              </div>
              <div className="mt-0.5 flex flex-wrap gap-1">
                {entry.risk_queries_as_target.map((seq) => (
                  <span
                    key={`t-${seq}`}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-violet-950/30 border border-violet-700/40 text-violet-300"
                    title="Was the target of this risk query"
                  >
                    #{seq} (target)
                  </span>
                ))}
                {entry.risk_queries_as_concern.map((seq) => (
                  <span
                    key={`c-${seq}`}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-amber-950/30 border border-amber-700/40 text-amber-300"
                    title="Was flagged as a concern in this risk query"
                  >
                    #{seq} (concern)
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Technical detail — collapsible. */}
          {entry.technical && (
            <div>
              <button
                type="button"
                onClick={() => setShowTech((v) => !v)}
                className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300"
              >
                {showTech ? (
                  <ChevronDown className="h-3 w-3" />
                ) : (
                  <ChevronRight className="h-3 w-3" />
                )}
                {showTech ? "Hide" : "Show"} technical detail
              </button>
              {showTech && (
                <div className="mt-1 text-[11px] leading-relaxed border-l-2 border-violet-900/40 pl-2 text-muted-foreground">
                  {entry.technical}
                </div>
              )}
            </div>
          )}

          {/* Symbol drill-down — function/class/method-level memory.
              Toggle is lazy: we don't fetch symbols until the user
              clicks. Result caches in component state so re-toggling
              is cheap. */}
          <div>
            <button
              type="button"
              onClick={handleToggleSymbols}
              className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300"
            >
              {showSymbols ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              <Code2 className="h-3 w-3" />
              <span>
                {showSymbols ? "Hide" : "Show"} symbols
                {symbols !== null && (
                  <span className="text-muted-foreground/70 ml-1">
                    ({symbols.length})
                  </span>
                )}
              </span>
            </button>
            {showSymbols && (
              <div className="mt-1.5 ml-3 space-y-1">
                {symbolsLoading && (
                  <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Loading symbols…
                  </div>
                )}
                {symbolsError && (
                  <div className="text-[10px] text-red-300">{symbolsError}</div>
                )}
                {!symbolsLoading && symbols !== null && symbols.length === 0 && (
                  <div className="text-[10px] text-muted-foreground leading-relaxed">
                    No symbols indexed for this file yet. Either symbol
                    extraction isn't supported for this language, or the
                    guardian's per-symbol pass is still running in the
                    background.
                  </div>
                )}
                {!symbolsLoading && symbols && symbols.length > 0 && (
                  <div className="space-y-1">
                    {symbols.map((sym) => (
                      <SymbolRow
                        key={`${sym.symbol_kind}:${sym.qualified_name}:${sym.start_line}`}
                        symbol={sym}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Metadata footer — when indexed, by which model. */}
          <div className="flex items-center gap-2 pt-1 border-t border-border text-[9px] text-muted-foreground/60">
            <span>
              indexed {formatRelativeTime(
                new Date(entry.indexed_at * 1000).toISOString(),
              )}
            </span>
            <span>·</span>
            <span>{entry.indexer_model}</span>
            <span>·</span>
            <span className="tabular-nums">
              {(entry.input_tokens + entry.output_tokens).toLocaleString()} tok
            </span>
          </div>
        </div>
      )}
    </div>
  );
}


function SymbolRow({ symbol }: { symbol: SymbolSummary }) {
  const [expanded, setExpanded] = useState(false);
  const totalRiskNotes = symbol.risk_notes?.length ?? 0;

  // Color the kind badge per-kind so users can tell functions from
  // classes from constants at a glance.
  const kindClass = kindBadgeClass(symbol.symbol_kind);

  return (
    <div className="rounded border border-border bg-background/30">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-start gap-2 px-2 py-1.5 text-left min-w-0"
      >
        {expanded ? (
          <ChevronDown className="mt-0.5 h-2.5 w-2.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-2.5 w-2.5 shrink-0 text-muted-foreground" />
        )}
        <span
          className={cn(
            "text-[9px] px-1 rounded border flex-shrink-0 uppercase tracking-wide",
            kindClass,
          )}
        >
          {symbol.symbol_kind}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <code className="text-[10px] font-medium break-all">
              {symbol.qualified_name}
            </code>
            <span className="text-[9px] text-muted-foreground/70 tabular-nums">
              L{symbol.start_line}-{symbol.end_line}
            </span>
            {totalRiskNotes > 0 && (
              <span
                className="text-[9px] px-1 rounded bg-amber-950/40 border border-amber-700/40 text-amber-300"
                title={`${totalRiskNotes} risk note(s) on this symbol`}
              >
                {totalRiskNotes} risk
              </span>
            )}
          </div>
          {!expanded && symbol.purpose && (
            <div className="mt-0.5 truncate text-[10px] text-muted-foreground">
              {symbol.purpose}
            </div>
          )}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border px-2 py-1.5 text-[10px] space-y-1.5">
          {symbol.plain_english && (
            <div className="leading-relaxed">{symbol.plain_english}</div>
          )}
          {symbol.touches && symbol.touches.length > 0 && (
            <LabeledChips label="Touches" items={symbol.touches} />
          )}
          {symbol.assumes && symbol.assumes.length > 0 && (
            <LabeledChips label="Assumes" items={symbol.assumes} />
          )}
          {symbol.failure_modes && symbol.failure_modes.length > 0 && (
            <LabeledChips
              label="Failure modes"
              items={symbol.failure_modes}
              chipClass="text-amber-200 border-amber-900/40 bg-amber-950/20"
            />
          )}
          {symbol.risk_notes && symbol.risk_notes.length > 0 && (
            <div>
              <div className="text-[8px] uppercase tracking-wide text-amber-400">
                Risk notes
              </div>
              <ul className="mt-0.5 space-y-0.5 list-disc list-inside text-[10px] text-amber-200">
                {symbol.risk_notes.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


function kindBadgeClass(kind: string): string {
  switch (kind) {
    case "function":
      return "text-emerald-300 border-emerald-900/40 bg-emerald-950/30";
    case "method":
      return "text-cyan-300 border-cyan-900/40 bg-cyan-950/30";
    case "class":
    case "struct":
      return "text-violet-300 border-violet-900/40 bg-violet-950/30";
    case "interface":
    case "trait":
      return "text-indigo-300 border-indigo-900/40 bg-indigo-950/30";
    case "enum":
      return "text-fuchsia-300 border-fuchsia-900/40 bg-fuchsia-950/30";
    case "constant":
      return "text-amber-300 border-amber-900/40 bg-amber-950/30";
    case "module":
      return "text-sky-300 border-sky-900/40 bg-sky-950/30";
    default:
      return "text-muted-foreground border-border bg-background/60";
  }
}


function LabeledChips({
  label, items, chipClass,
}: {
  label: string;
  items: string[];
  chipClass?: string;
}) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 flex flex-wrap gap-1">
        {items.map((item, i) => (
          <span
            key={i}
            className={cn(
              "text-[10px] px-1.5 py-0.5 rounded border break-all",
              chipClass ?? "border-border bg-background/60",
            )}
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}
