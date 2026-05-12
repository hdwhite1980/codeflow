import { NewProjectForm } from "@/components/new-project-form";
import { ProjectCard } from "@/components/project-card";
import { listProjects } from "@/lib/api";

/**
 * Home page. Server component — fetches the project list at request
 * time and renders. The new-project form is a client component that
 * handles its own POST, then router.push()es to the detail page.
 *
 * No revalidation strategy needed: each navigation triggers a fresh
 * server render (cache: 'no-store' in the API client).
 */
export default async function HomePage() {
  let projects: Awaited<ReturnType<typeof listProjects>>["projects"] = [];
  let listError: string | null = null;

  try {
    const resp = await listProjects(50);
    projects = resp.projects;
  } catch (err) {
    listError =
      err instanceof Error ? err.message : "Failed to load projects.";
  }

  return (
    <div className="grid gap-8 md:grid-cols-[2fr_3fr]">
      <section>
        <NewProjectForm />
      </section>
      <section>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Recent builds
        </h2>
        {listError ? (
          <div className="rounded-md border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-300">
            {listError}
          </div>
        ) : projects.length === 0 ? (
          <div className="rounded-md border border-border bg-card/40 p-6 text-center text-sm text-muted-foreground">
            No builds yet. Start one to see it appear here.
          </div>
        ) : (
          <div className="space-y-3">
            {projects.map((p) => (
              <ProjectCard key={p.id} project={p} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
