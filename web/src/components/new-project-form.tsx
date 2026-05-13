"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Plus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { createProject } from "@/lib/api";
import {
  ALLOWED_SERVICE_KINDS,
  type ServiceConfig,
  type ServiceKind,
} from "@/lib/types";

/**
 * New-build form.
 *
 * Form shape now includes a Services section. Backend requires at
 * least one service entry before it accepts the project. The user
 * picks the kind from a dropdown and supplies a label (mandatory)
 * and an optional config string.
 *
 * Validation
 * ----------
 *   - slug: 2-64 chars
 *   - prompt: 10-4000 chars
 *   - services: 1-20 entries, each with non-empty label
 *
 * Errors are surfaced inline so the user doesn't have to round-trip
 * through the backend for the obvious cases.
 */

const KIND_LABELS: Record<ServiceKind, string> = {
  postgres: "Postgres",
  mysql: "MySQL",
  redis: "Redis",
  railway: "Railway",
  vercel: "Vercel",
  github: "GitHub",
  stripe: "Stripe",
  anthropic: "Anthropic API",
  openai: "OpenAI API",
  other: "Other",
};

interface ServiceRowState extends ServiceConfig {
  /** Local-only id for React key stability across add/remove. */
  _key: string;
}

let _serviceRowCounter = 0;
function makeRow(initial?: Partial<ServiceRowState>): ServiceRowState {
  _serviceRowCounter += 1;
  return {
    _key: `svc-${_serviceRowCounter}`,
    kind: initial?.kind ?? "postgres",
    label: initial?.label ?? "",
    config: initial?.config ?? "",
  };
}

export function NewProjectForm() {
  const router = useRouter();
  const [slug, setSlug] = useState("");
  const [prompt, setPrompt] = useState("");
  // Start with one service row to nudge the user — the backend rejects
  // empty arrays anyway.
  const [services, setServices] = useState<ServiceRowState[]>([
    makeRow({ kind: "postgres", label: "Main database" }),
  ]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function validate(): string | null {
    if (slug.trim().length < 2) return "Slug must be at least 2 characters.";
    if (slug.trim().length > 64) return "Slug must be 64 characters or fewer.";
    if (prompt.trim().length < 10)
      return "Prompt must be at least 10 characters.";
    if (prompt.trim().length > 4000)
      return "Prompt must be 4000 characters or fewer.";
    if (services.length === 0)
      return "Add at least one service this build will use.";
    for (const s of services) {
      if (s.label.trim().length === 0)
        return "Every service needs a label.";
      if (s.label.length > 128)
        return "Service labels must be 128 characters or fewer.";
    }
    return null;
  }

  function updateService(key: string, patch: Partial<ServiceRowState>) {
    setServices((prev) =>
      prev.map((s) => (s._key === key ? { ...s, ...patch } : s)),
    );
  }

  function addService() {
    setServices((prev) => [...prev, makeRow()]);
  }

  function removeService(key: string) {
    setServices((prev) =>
      prev.length === 1 ? prev : prev.filter((s) => s._key !== key),
    );
  }

  async function onSubmit(ev: React.FormEvent) {
    ev.preventDefault();
    setError(null);
    const v = validate();
    if (v) {
      setError(v);
      return;
    }
    setSubmitting(true);
    try {
      // Strip the internal _key field before sending to the backend.
      const cleanServices: ServiceConfig[] = services.map((s) => ({
        kind: s.kind,
        label: s.label.trim(),
        config: s.config?.trim() ? s.config.trim() : null,
      }));
      const resp = await createProject({
        slug: slug.trim(),
        prompt: prompt.trim(),
        services: cleanServices,
      });
      router.push(`/projects/${resp.project_id}`);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to create project. Try again.",
      );
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">New build</CardTitle>
        <CardDescription>
          Describe what you want built. The builder generates the files,
          then two independent auditors review them in parallel.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-5">
          <div className="space-y-2">
            <label
              htmlFor="slug"
              className="text-sm font-medium text-muted-foreground"
            >
              Project slug
            </label>
            <Input
              id="slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="csv-converter"
              disabled={submitting}
            />
            <p className="text-xs text-muted-foreground">
              A short identifier for this build (2–64 chars, unique).
            </p>
          </div>

          <div className="space-y-2">
            <label
              htmlFor="prompt"
              className="text-sm font-medium text-muted-foreground"
            >
              Prompt
            </label>
            <Textarea
              id="prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="A Python FastAPI service that exposes a /convert endpoint accepting CSV uploads..."
              rows={6}
              disabled={submitting}
            />
            <p className="text-xs text-muted-foreground">
              10–4000 characters. Be specific about what files,
              endpoints, and error handling you want.
            </p>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium text-muted-foreground">
                Services
              </label>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={addService}
                disabled={submitting || services.length >= 20}
              >
                <Plus className="mr-1 h-3 w-3" />
                Add service
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Anchor nodes for the graph view. At least one required.
            </p>
            <div className="space-y-2">
              {services.map((s) => (
                <ServiceRow
                  key={s._key}
                  row={s}
                  onChange={(patch) => updateService(s._key, patch)}
                  onRemove={() => removeService(s._key)}
                  canRemove={services.length > 1}
                  disabled={submitting}
                />
              ))}
            </div>
          </div>

          {error && (
            <div className="rounded-md border border-red-900/40 bg-red-950/30 px-3 py-2 text-sm text-red-300">
              {error}
            </div>
          )}
          <Button type="submit" disabled={submitting}>
            {submitting ? "Starting build…" : "Start build"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

interface ServiceRowProps {
  row: ServiceRowState;
  onChange: (patch: Partial<ServiceRowState>) => void;
  onRemove: () => void;
  canRemove: boolean;
  disabled: boolean;
}

function ServiceRow({
  row,
  onChange,
  onRemove,
  canRemove,
  disabled,
}: ServiceRowProps) {
  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex gap-2">
        <select
          value={row.kind}
          onChange={(e) =>
            onChange({ kind: e.target.value as ServiceKind })
          }
          disabled={disabled}
          className="h-9 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {ALLOWED_SERVICE_KINDS.map((k) => (
            <option key={k} value={k}>
              {KIND_LABELS[k]}
            </option>
          ))}
        </select>
        <Input
          value={row.label}
          onChange={(e) => onChange({ label: e.target.value })}
          placeholder="Label (e.g. 'Main DB')"
          disabled={disabled}
          className="h-9 flex-1"
        />
        {canRemove && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onRemove}
            disabled={disabled}
            className="h-9 w-9 shrink-0"
            aria-label="Remove service"
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>
      <Input
        value={row.config ?? ""}
        onChange={(e) => onChange({ config: e.target.value })}
        placeholder="Optional config (URL, repo, env var name)"
        disabled={disabled}
        className="h-8 text-xs"
      />
    </div>
  );
}
