"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Github, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { importProject } from "@/lib/api";

/**
 * Import-existing-repo form. Lives alongside the new-project form
 * on the home page.
 *
 * Workflow
 * --------
 * 1. User pastes a GitHub URL (or owner/repo shorthand).
 * 2. Submit → POST /api/projects/import → returns project_id.
 * 3. Router pushes to /projects/<id>. The detail page will show
 *    "importing..." until the worker finishes cloning + writing
 *    files, then transitions to normal project view.
 *
 * Public repos only this version. Private repo support needs OAuth.
 */
export function ImportProjectForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError(null);

    const trimmed = url.trim();
    if (trimmed.length < 3) {
      setError("Please enter a repository URL.");
      return;
    }

    setSubmitting(true);
    try {
      const resp = await importProject({ url: trimmed });
      router.push(`/projects/${resp.project_id}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Import failed";
      setError(msg);
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Github className="h-4 w-4" />
          Import existing repo
        </CardTitle>
        <CardDescription>
          Point at a public GitHub repo. We clone it, index every file
          with the guardian, and you can iterate with full memory of the
          codebase.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className="block text-xs text-muted-foreground mb-1">
              Repository URL
            </label>
            <Input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://github.com/owner/repo  or  owner/repo"
              disabled={submitting}
              maxLength={500}
            />
            <p className="mt-1 text-[10px] text-muted-foreground">
              Public repos only. Limit: 500 files, 50MB total.
            </p>
          </div>

          {error && (
            <div className="rounded border border-red-900/40 bg-red-950/30 p-2 text-xs text-red-300">
              {error}
            </div>
          )}

          <Button
            type="submit"
            disabled={submitting || url.trim().length < 3}
            className="w-full"
          >
            {submitting ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Queueing import…
              </>
            ) : (
              "Import"
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
