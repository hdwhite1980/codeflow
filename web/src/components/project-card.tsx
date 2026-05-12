import Link from "next/link";

import { Card, CardContent } from "@/components/ui/card";
import { formatRelativeTime } from "@/lib/format";
import type { ProjectSummary } from "@/lib/types";

interface ProjectCardProps {
  project: ProjectSummary;
}

/**
 * Single-line summary card in the project list. Click navigates to
 * the detail page. We deliberately don't pre-fetch cost/findings here —
 * the list page would be much slower if we did, and the user usually
 * clicks through to see the details anyway.
 */
export function ProjectCard({ project }: ProjectCardProps) {
  return (
    <Link
      href={`/projects/${project.id}`}
      className="block transition-opacity hover:opacity-80"
    >
      <Card>
        <CardContent className="flex items-center justify-between p-4">
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
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
