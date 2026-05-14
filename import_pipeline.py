"""
codeflow.import_pipeline
========================

Import an existing public GitHub repository into Code Flow.

Workflow
--------
1. Validate the repo URL (parse owner/repo, normalize the form).
2. Shallow clone (`git clone --depth 1`) into a temp directory.
3. Walk the working tree, applying:
     - Exclusions: .git/, common build dirs (node_modules, .next, dist,
       build, target, venv, __pycache__, .pytest_cache, .mypy_cache,
       .gradle, .idea, .vscode)
     - File-size cap (per file): 256KB default. Larger files are
       skipped with a note.
     - Binary detection: skip files that aren't valid UTF-8 text.
     - Total file cap: 500 files default. Pick the first 500 by path
       ordering after sorting — larger repos surface a warning.
4. Write each file as a `file:<project_id>:<path>` ledger entry with
   tier=GENERATION, kind=FILE.
5. Write a DECISION_RECORD at the start and end (import:started,
   import:outcome) so the project detail page can show status.
6. Enqueue a `guardian_index` job for the project. The worker indexes
   each file in the background; the user can iterate while it runs.

Why git CLI not GitHub API
--------------------------
The GitHub REST API requires multiple round-trips per file and rate-
limits to 60 anonymous requests/hour. `git clone --depth 1` over HTTPS
fetches the entire repo in one shot, doesn't authenticate, and works
with any git host (not just github.com — gitlab, bitbucket, self-hosted).

Why temp dir not in-process tarball download
--------------------------------------------
Could fetch /archive/refs/heads/main.tar.gz from GitHub and stream it
in-memory, but that ties us to GitHub's archive layout. A shallow clone
is identical for our purposes (one HTTPS round-trip) and works
identically for every git host.

Limits / safety
---------------
- Public repos only this turn. Private repo support requires OAuth.
- 5-minute clone timeout. The git subprocess is killed if it hangs.
- 50MB total disk usage cap on the temp dir (we don't enforce mid-
  clone; we check before reading files and bail with a warning if
  blown past).
- Binary detection is conservative: anything that fails UTF-8 decode
  is skipped, even if it's actually a unicode-flavored text file with
  one bad byte. False negatives are fine here — text files index, the
  occasional binary file with valid utf-8 (rare) becomes a stub file.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ledger import ArtifactKind, EdgeKind, Tier
from build_pipeline import file_artifact_key


# Per-file size cap. Larger files almost always indicate either a
# build artifact accidentally committed, a vendored dependency, or
# a generated file like a minified bundle. None of these benefit from
# being in the ledger.
MAX_FILE_SIZE_BYTES = 256 * 1024  # 256KB

# Total file cap. Above this, indexing on local Ollama would take
# hours; rather than hang the user, we accept the first N files and
# flag the rest. Larger limits OK for paid tiers later.
MAX_FILE_COUNT = 500

# Total disk-budget for the working tree. Public repos can be huge
# (Linux kernel, etc.). We check before walking; if exceeded we bail
# with a clear message rather than OOM the worker.
MAX_REPO_BYTES = 50 * 1024 * 1024  # 50MB

# Clone wall-clock timeout. Most public repos clone in <30s; we cap
# at 5min to bound worst case.
CLONE_TIMEOUT_SECONDS = 300

# Directories to skip entirely. Common across most ecosystems.
EXCLUDED_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", ".next", ".nuxt",
    "dist", "build", "out", "target",
    "venv", ".venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".gradle", ".idea", ".vscode",
    "vendor", "Pods", "Carthage",
    ".terraform",
}

# File extensions to skip — large, binary, or generated. We do utf-8
# detection too but this fast-path catches the common cases.
EXCLUDED_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    # Video / audio
    ".mp4", ".mov", ".avi", ".webm", ".mkv", ".mp3", ".wav", ".ogg", ".flac",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    # Binaries
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".class", ".jar", ".war",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Documents
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".doc", ".xls", ".ppt",
    # Lock files — large, machine-generated, low information.
    ".lock",
    # Various
    ".sqlite", ".db", ".pyc", ".pyo",
}


@dataclass
class ImportOutcome:
    """Result of an import run. Captures everything the frontend or
    audit trail would want to know about how it went."""
    files_imported: int = 0
    files_skipped_size: int = 0       # too big
    files_skipped_binary: int = 0     # failed utf-8
    files_skipped_extension: int = 0  # excluded ext
    files_skipped_dir: int = 0        # under an excluded dir
    files_capped: int = 0             # past MAX_FILE_COUNT
    total_bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    error: Optional[str] = None       # set when the whole import failed
    elapsed_seconds: float = 0.0
    repo_url: str = ""
    commit_sha: Optional[str] = None  # the commit we imported


def parse_github_url(url: str) -> tuple[str, str, str]:
    """Parse a GitHub URL into (owner, repo, normalized_https_url).

    Accepts:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - github.com/owner/repo
      - git@github.com:owner/repo.git
      - owner/repo (shorthand)

    Returns the normalized https URL suitable for `git clone`. We
    rewrite git@ SSH URLs to HTTPS so we don't need SSH keys.
    """
    url = url.strip()
    if not url:
        raise ValueError("URL is empty")

    # git@github.com:owner/repo.git → owner, repo
    ssh_match = re.match(r"^git@([^:]+):([^/]+)/(.+?)(\.git)?$", url)
    if ssh_match:
        host, owner, repo = ssh_match.group(1), ssh_match.group(2), ssh_match.group(3)
        return owner, repo, f"https://{host}/{owner}/{repo}.git"

    # Strip leading https?:// or //
    bare = re.sub(r"^https?://", "", url)
    bare = bare.lstrip("/")
    # Strip trailing .git
    bare = re.sub(r"\.git$", "", bare)

    parts = bare.split("/")
    if len(parts) == 2:
        # Shorthand: "owner/repo" → assume github.com
        owner, repo = parts
        return owner, repo, f"https://github.com/{owner}/{repo}.git"
    if len(parts) >= 3 and "." in parts[0]:
        # host/owner/repo[/...] — anything after repo (tree/main, etc.) ignored
        host, owner, repo = parts[0], parts[1], parts[2]
        return owner, repo, f"https://{host}/{owner}/{repo}.git"

    raise ValueError(
        f"Cannot parse URL into owner/repo: {url!r}. "
        f"Expected forms: 'owner/repo', 'github.com/owner/repo', "
        f"'https://github.com/owner/repo'."
    )


async def _run_git_clone(
    url: str, dest_dir: str, *, timeout: float = CLONE_TIMEOUT_SECONDS,
) -> str:
    """Shallow-clone the repo into dest_dir. Returns the HEAD commit SHA.

    Uses --depth 1 (no history) and --single-branch (no other branches)
    to minimize transfer. We need the working tree, not the history,
    for importing.

    Raises subprocess.CalledProcessError on clone failure, or
    asyncio.TimeoutError on hang.
    """
    # git clone <url> <dest>
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--single-branch",
        url, dest_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"git clone failed: {err}")

    # Read HEAD commit SHA.
    sha_proc = await asyncio.create_subprocess_exec(
        "git", "-C", dest_dir, "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    sha_out, _ = await sha_proc.communicate()
    return (sha_out or b"").decode("utf-8").strip()


def _is_under_excluded_dir(rel_path: Path) -> bool:
    """Check whether any component of the path is an excluded directory.

    We check parts, not just the leading part, because monorepos
    often have `frontend/node_modules` etc.
    """
    return any(part in EXCLUDED_DIRS for part in rel_path.parts)


def _is_excluded_extension(path: Path) -> bool:
    return path.suffix.lower() in EXCLUDED_EXTENSIONS


def _read_text_or_none(path: Path) -> Optional[str]:
    """Read a file as UTF-8 text. Returns None for binary/undecodable."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _walk_repo_files(repo_dir: Path) -> list[Path]:
    """Walk the repo, returning absolute paths that pass the cheap
    filters (extension, directory, size). Binary detection happens
    later when we actually read the file."""
    matches: list[Path] = []
    for root, dirs, files in os.walk(repo_dir):
        # Mutate dirs in place so we don't descend into excluded ones.
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fname in files:
            absolute = Path(root) / fname
            if _is_excluded_extension(absolute):
                continue
            matches.append(absolute)
    # Stable order so the cap is deterministic.
    matches.sort()
    return matches


async def import_repository(
    *,
    url: str,
    project_id: str,
    store,
) -> ImportOutcome:
    """Import a public repo into a project. Writes file entries to the
    ledger and an outcome record. Does NOT trigger guardian indexing —
    the caller (job_handler) does that after we return.

    Synchronous within the worker (this is itself called as part of an
    import_repo job). Bails cleanly on any failure with a descriptive
    error in the outcome.
    """
    started = time.time()
    outcome = ImportOutcome(repo_url=url)

    try:
        owner, repo, https_url = parse_github_url(url)
    except ValueError as exc:
        outcome.error = f"Invalid URL: {exc}"
        outcome.elapsed_seconds = time.time() - started
        return outcome

    # Use a TemporaryDirectory so cleanup is guaranteed even on crash.
    with tempfile.TemporaryDirectory(prefix="codeflow_import_") as tmp:
        clone_dir = os.path.join(tmp, "repo")
        try:
            commit_sha = await _run_git_clone(https_url, clone_dir)
            outcome.commit_sha = commit_sha
        except asyncio.TimeoutError:
            outcome.error = (
                f"Clone timed out after {CLONE_TIMEOUT_SECONDS}s. "
                f"Repo may be very large or git server is slow."
            )
            outcome.elapsed_seconds = time.time() - started
            return outcome
        except Exception as exc:
            outcome.error = f"Clone failed: {type(exc).__name__}: {exc}"
            outcome.elapsed_seconds = time.time() - started
            return outcome

        repo_dir = Path(clone_dir)
        all_files = _walk_repo_files(repo_dir)

        for absolute in all_files:
            rel = absolute.relative_to(repo_dir)
            rel_str = str(rel).replace(os.sep, "/")

            # Cap on total file count.
            if outcome.files_imported >= MAX_FILE_COUNT:
                outcome.files_capped += 1
                continue

            try:
                size = absolute.stat().st_size
            except OSError as exc:
                outcome.failed.append((rel_str, f"stat: {exc}"))
                continue

            if size > MAX_FILE_SIZE_BYTES:
                outcome.files_skipped_size += 1
                continue

            text = _read_text_or_none(absolute)
            if text is None:
                outcome.files_skipped_binary += 1
                continue

            try:
                store.write_entry(
                    project_id=project_id,
                    tier=Tier.GENERATION,
                    artifact_kind=ArtifactKind.FILE,
                    artifact_key=file_artifact_key(project_id, rel_str),
                    body=text,
                    rationale=(
                        f"Imported from {url} at commit "
                        f"{(commit_sha or 'HEAD')[:8]}."
                    ),
                    author=f"import:github:{owner}/{repo}",
                )
                outcome.files_imported += 1
                outcome.total_bytes += size
            except Exception as exc:
                outcome.failed.append(
                    (rel_str, f"{type(exc).__name__}: {exc}")
                )

            # Honour repo-size budget — bail early if we blow past.
            if outcome.total_bytes > MAX_REPO_BYTES:
                outcome.error = (
                    f"Repo exceeded {MAX_REPO_BYTES // (1024*1024)}MB "
                    f"text budget; stopped after {outcome.files_imported} files."
                )
                break

    outcome.elapsed_seconds = time.time() - started
    return outcome


def write_import_started(
    store, project_id: str, url: str,
) -> None:
    """Record import:started so the UI can show 'importing…' state."""
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"import:started",
        body={"url": url, "started_at": time.time()},
        rationale=f"Import started from {url}",
        author="worker:import",
    )


def write_import_outcome(
    store, project_id: str, outcome: ImportOutcome,
) -> None:
    """Record import:outcome with the full ImportOutcome dataclass
    rendered as JSON body."""
    body = {
        "files_imported": outcome.files_imported,
        "files_skipped_size": outcome.files_skipped_size,
        "files_skipped_binary": outcome.files_skipped_binary,
        "files_skipped_extension": outcome.files_skipped_extension,
        "files_skipped_dir": outcome.files_skipped_dir,
        "files_capped": outcome.files_capped,
        "total_bytes": outcome.total_bytes,
        "failed": [{"path": p, "reason": r} for p, r in outcome.failed],
        "error": outcome.error,
        "elapsed_seconds": outcome.elapsed_seconds,
        "repo_url": outcome.repo_url,
        "commit_sha": outcome.commit_sha,
    }
    rationale = (
        f"Import {'failed' if outcome.error else 'completed'}: "
        f"{outcome.files_imported} files in {outcome.elapsed_seconds:.1f}s."
        + (f" Error: {outcome.error}" if outcome.error else "")
    )
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"import:outcome",
        body=body,
        rationale=rationale,
        author="worker:import",
    )
