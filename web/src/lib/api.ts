/**
 * REST client for the FastAPI backend.
 *
 * All functions in this module:
 *   - read `NEXT_PUBLIC_API_URL` from env
 *   - throw on non-2xx (with the response body in the error)
 *   - return parsed JSON typed per the interfaces in ./types
 *
 * The API URL is `NEXT_PUBLIC_*` because the browser needs it; Next.js
 * inlines NEXT_PUBLIC_ vars at build time. If we ever add server-only
 * routes that call the API server-side, we'd duplicate the URL into a
 * non-public env var; for now everything is client-side.
 */

import type {
  ArtifactListResponse,
  AuditResponse,
  CreateProjectRequest,
  CreateProjectResponse,
  FixAllEstimate,
  FixAllPassesResponse,
  FixAllResponse,
  GraphResponse,
  Iteration,
  IterateRequest,
  IterateResponse,
  IterationListResponse,
  ProjectListResponse,
  UsageSummary,
} from "./types";

function apiBase(): string {
  const url = process.env.NEXT_PUBLIC_API_URL;
  if (!url) {
    // Throw a clear message rather than silently using a default; the
    // frontend is useless without the API and we want failures loud.
    throw new Error(
      "NEXT_PUBLIC_API_URL is not set. " +
        "Set it on the Railway web service to the FastAPI public URL " +
        "(e.g. https://codeflow-production-27c6.up.railway.app).",
    );
  }
  // Strip trailing slash so callers can rely on `${base}/path` shape.
  return url.replace(/\/$/, "");
}

class ApiError extends Error {
  constructor(
    public status: number,
    public body: string,
    public endpoint: string,
  ) {
    super(`${endpoint} returned ${status}: ${body.slice(0, 200)}`);
  }
}

async function getJSON<T>(path: string): Promise<T> {
  const url = `${apiBase()}${path}`;
  const res = await fetch(url, {
    // Always go fresh for project detail pages — we want the latest
    // ledger state when a user lands on /projects/<id>. Next.js' default
    // is to cache GETs aggressively in server components, which would
    // show stale data.
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiError(res.status, body, path);
  }
  return res.json();
}

async function postJSON<TBody, TResp>(
  path: string,
  body: TBody,
): Promise<TResp> {
  const url = `${apiBase()}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const errBody = await res.text().catch(() => "");
    throw new ApiError(res.status, errBody, path);
  }
  return res.json();
}

// -- Project list / create -----------------------------------------

export async function listProjects(limit = 50): Promise<ProjectListResponse> {
  return getJSON<ProjectListResponse>(
    `/api/projects?limit=${encodeURIComponent(limit)}`,
  );
}

export async function createProject(
  body: CreateProjectRequest,
): Promise<CreateProjectResponse> {
  return postJSON<CreateProjectRequest, CreateProjectResponse>(
    "/api/projects",
    body,
  );
}

// -- Per-project endpoints -----------------------------------------

export async function getArtifacts(
  projectId: string,
): Promise<ArtifactListResponse> {
  return getJSON<ArtifactListResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/artifacts`,
  );
}

export async function getUsage(projectId: string): Promise<UsageSummary> {
  return getJSON<UsageSummary>(
    `/api/projects/${encodeURIComponent(projectId)}/usage`,
  );
}

export async function getAudits(projectId: string): Promise<AuditResponse> {
  return getJSON<AuditResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/audits`,
  );
}

export async function getGraph(projectId: string): Promise<GraphResponse> {
  return getJSON<GraphResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/graph`,
  );
}

// -- Iteration ------------------------------------------------------

export async function iterateProject(
  projectId: string,
  body: IterateRequest,
): Promise<IterateResponse> {
  return postJSON<IterateRequest, IterateResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/iterate`,
    body,
  );
}

export async function getIterations(
  projectId: string,
): Promise<IterationListResponse> {
  return getJSON<IterationListResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/iterations`,
  );
}

// -- Fix-all --------------------------------------------------------

export async function getFixAllEstimate(
  projectId: string,
): Promise<FixAllEstimate> {
  return getJSON<FixAllEstimate>(
    `/api/projects/${encodeURIComponent(projectId)}/fix-all/estimate`,
  );
}

export async function triggerFixAll(
  projectId: string,
): Promise<FixAllResponse> {
  return postJSON<Record<string, never>, FixAllResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/fix-all`,
    {},
  );
}

export async function getFixAllPasses(
  projectId: string,
): Promise<FixAllPassesResponse> {
  return getJSON<FixAllPassesResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/fix-all/passes`,
  );
}

// -- WebSocket URL builder -----------------------------------------

/**
 * Build the WS URL for one project. Reads NEXT_PUBLIC_API_URL and
 * substitutes https→wss / http→ws. Doesn't open the socket; the
 * caller (useProjectStream) does.
 */
export function projectWebsocketUrl(projectId: string): string {
  const base = apiBase();
  const wsBase = base
    .replace(/^https:/, "wss:")
    .replace(/^http:/, "ws:");
  return `${wsBase}/ws/projects/${encodeURIComponent(projectId)}`;
}

export { ApiError };
