"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Loader2, Trash2 } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { deleteProject } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ProjectSummary } from "@/lib/types";

interface ProjectCardProps {
  project: ProjectSummary;
}

/**
 * Single-line summary card in the project list. Click navigates to
 * the detail page. We deliberately don't pre-fetch cost/findings here —
 * the list page would be much slower if we did, and the user usually
 * clicks through to see the details anyway.
 *
 * Delete affordance (Turn G-stop-delete)
 * ---------------------------------------
 * A trash icon lives in the top-right of each card. First click flips
 * to a confirm state (red "Delete?" + checkmark/x); second click on
 * the checkmark actually deletes. This in-card confirm avoids using
 * window.confirm() (which is uglier and harder to style) without
 * needing a full modal component.
 *
 * The whole card is wrapped in an anchor for navigation, but the
 * delete-related controls call stopPropagation so they don't
 * accidentally navigate.
 */
export function ProjectCard({ project }: ProjectCardProps) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDelete(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDeleting(true);
    setError(null);
    try {
      await deleteProject(project.id);
      // Server component re-renders with the project removed from the
      // listProjects() result.
      router.refresh();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Delete failed",
      );
      setDeleting(false);
      setConfirming(false);
    }
    // No finally-resetting setDeleting(false) on success — the card
    // is about to unmount as part of the refresh.
  }

  function handleConfirmStart(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setConfirming(true);
  }

  function handleCancelConfirm(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setConfirming(false);
    setError(null);
  }

  return (
    <div className="relative">
      <Link
        href={`/projects/${project.id}`}
        className={cn(
          "block transition-opacity hover:opacity-80",
          deleting && "pointer-events-none opacity-50",
        )}
      >
        <Card>
          <CardContent className="flex items-center justify-between p-4 pr-12">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="flex items-center gap-3">
                <code className="text-sm font-medium">{project.slug}</code>
                <span className="text-xs text-muted-foreground">
                  {formatRelativeTime(project.created_at)}
                </span>
              </div>
              <p className="truncate text-sm text-muted-foreground">
                {project.prompt}
              </p>
              {error && (
                <p className="text-xs text-red-300">{error}</p>
              )}
            </div>
          </CardContent>
        </Card>
      </Link>

      {/* Delete controls — overlay in the top-right of the card.
          Outside the Link so clicks don't navigate. */}
      <div className="absolute right-3 top-3 flex items-center gap-1">
        {!confirming && (
          <button
            type="button"
            onClick={handleConfirmStart}
            disabled={deleting}
            title="Delete project"
            className="rounded p-1.5 text-muted-foreground hover:text-red-400 hover:bg-red-950/30 disabled:opacity-50"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
        {confirming && !deleting && (
          <>
            <span className="text-[11px] font-medium text-red-300 mr-1">
              Delete?
            </span>
            <button
              type="button"
              onClick={handleDelete}
              title="Confirm delete"
              className="rounded border border-red-700/50 bg-red-950/40 px-2 py-1 text-[11px] font-medium text-red-200 hover:bg-red-900/40"
            >
              Yes
            </button>
            <button
              type="button"
              onClick={handleCancelConfirm}
              title="Cancel"
              className="rounded border border-border bg-background/60 px-2 py-1 text-[11px] hover:bg-background/80"
            >
              No
            </button>
          </>
        )}
        {deleting && (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        )}
      </div>
    </div>
  );
}
