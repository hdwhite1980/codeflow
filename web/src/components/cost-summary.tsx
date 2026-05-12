import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatMoney, formatTokens, providerLabel } from "@/lib/format";
import type { StageRollup, UsageSummary } from "@/lib/types";

interface CostSummaryProps {
  usage: UsageSummary;
}

/**
 * Cost ticker card. Renders the top-line total plus a per-provider
 * breakdown row. Designed to be updated live — accepts a fully
 * computed UsageSummary so the page can mutate the rows array as WS
 * events arrive and pass a freshly summarized object on each render.
 */
export function CostSummary({ usage }: CostSummaryProps) {
  const totalTokens = usage.total_tokens ?? 0;
  const callCount = usage.call_count ?? 0;

  // Sort providers by spend desc — most expensive on the left, most
  // visible. Stable order helps when values are updating live.
  const byProvider = [...(usage.by_provider ?? [])].sort(
    (a, b) => b.total_cost_usd - a.total_cost_usd,
  );

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-baseline justify-between">
          <CardTitle className="text-lg font-medium text-muted-foreground">
            Total cost
          </CardTitle>
          <div className="text-xs text-muted-foreground">
            {callCount} {callCount === 1 ? "API call" : "API calls"} ·{" "}
            {formatTokens(totalTokens)} tokens
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="text-4xl font-bold tabular-nums">
          {formatMoney(usage.total_cost_usd ?? 0)}
        </div>
        {byProvider.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2">
            {byProvider.map((p) => (
              <ProviderChip key={p.stage} rollup={p} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ProviderChip({ rollup }: { rollup: StageRollup }) {
  // by_provider rollups use the `stage` field for the provider name.
  // That's a backend quirk we live with — the rollup type is shared.
  const provider = rollup.stage;
  return (
    <div className="inline-flex items-center gap-2 rounded-md border border-border bg-card/60 px-3 py-1.5">
      <Badge variant="provider" className="font-normal">
        {providerLabel(provider)}
      </Badge>
      <span className="text-sm font-medium tabular-nums">
        {formatMoney(rollup.total_cost_usd)}
      </span>
      <span className="text-xs text-muted-foreground">
        {rollup.call_count}{" "}
        {rollup.call_count === 1 ? "call" : "calls"}
      </span>
    </div>
  );
}
