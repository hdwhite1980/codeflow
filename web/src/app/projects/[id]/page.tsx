import { notFound } from "next/navigation";

import { ProjectDetailClient } from "./project-detail-client";
import { getArtifacts, getAudits, getUsage, ApiError } from "@/lib/api";

interface PageProps {
  params: { id: string };
}

/**
 * Server-rendered shell. Fetches the three per-project endpoints in
 * parallel, then hands the initial state to a client component that
 * subscribes to live updates and merges them in.
 *
 * If any of the three fetches fails with 404 we 404; any other error
 * surfaces in the client component (so the user can see what's wrong
 * rather than a blank page).
 */
export default async function ProjectPage({ params }: PageProps) {
  const { id } = params;

  const [artifactsRes, usageRes, auditsRes] = await Promise.allSettled([
    getArtifacts(id),
    getUsage(id),
    getAudits(id),
  ]);

  // If artifacts 404s, the project doesn't exist.
  if (
    artifactsRes.status === "rejected" &&
    artifactsRes.reason instanceof ApiError &&
    artifactsRes.reason.status === 404
  ) {
    notFound();
  }

  const initialArtifacts =
    artifactsRes.status === "fulfilled" ? artifactsRes.value : null;
  const initialUsage =
    usageRes.status === "fulfilled" ? usageRes.value : null;
  const initialAudits =
    auditsRes.status === "fulfilled" ? auditsRes.value : null;

  const initialError =
    artifactsRes.status === "rejected"
      ? extractMessage(artifactsRes.reason)
      : null;

  return (
    <ProjectDetailClient
      projectId={id}
      initialArtifacts={initialArtifacts}
      initialUsage={initialUsage}
      initialAudits={initialAudits}
      initialError={initialError}
    />
  );
}

function extractMessage(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  return "Failed to load project.";
}
