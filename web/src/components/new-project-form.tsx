"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

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

/**
 * Slug + prompt form. Submitting POSTs to /api/projects and navigates
 * to the detail page where the live build view takes over.
 *
 * Validation
 * ----------
 * Backend rejects:
 *   - slug shorter than 2 or longer than 64 chars
 *   - prompt shorter than 10 or longer than 4000 chars
 * We do the same checks client-side for instant feedback.
 */
export function NewProjectForm() {
  const router = useRouter();
  const [slug, setSlug] = useState("");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function validate(): string | null {
    if (slug.trim().length < 2) return "Slug must be at least 2 characters.";
    if (slug.trim().length > 64) return "Slug must be 64 characters or fewer.";
    if (prompt.trim().length < 10)
      return "Prompt must be at least 10 characters.";
    if (prompt.trim().length > 4000)
      return "Prompt must be 4000 characters or fewer.";
    return null;
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
      const resp = await createProject({
        slug: slug.trim(),
        prompt: prompt.trim(),
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
          Describe what you want built. Anthropic generates the files,
          then OpenAI and Gemini audit them in parallel.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4">
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
              10–4000 characters. Be specific about what files, endpoints,
              and error handling you want.
            </p>
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
