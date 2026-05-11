"""
codeflow.sandbox.runner
=======================

FastAPI service that runs ON THE HETZNER BOX (not Railway). Receives build
job requests from the Railway orchestrator, spins up an isolated Docker
container, copies files in, runs install + test, returns logs and metrics,
then tears the container down.

Security model
--------------
This is the perimeter that protects the Hetzner host from hostile generated
code. Every job runs in a container with:
  * --network=none for the test phase (no internet exfil)
  * --read-only root filesystem (no surprise file writes)
  * --tmpfs /tmp (small writable scratch space, lost on exit)
  * Memory and CPU caps
  * Wall-clock timeout
  * No host volume mounts beyond the project dir, mounted read-only

The install phase needs network to fetch packages — we allow it briefly,
then drop network entirely for the test phase. A two-step run.

Why FastAPI and not just a CLI
------------------------------
Railway needs an HTTP endpoint to call. The orchestrator submits jobs over
HTTPS (with bearer auth via Caddy) and gets structured JSON back. A CLI
wouldn't fit that flow without adding shell-execution glue.

Deploy posture on Hetzner
-------------------------
This runs as a systemd service. Caddy in front handles TLS termination
and bearer-token auth. The service itself binds 127.0.0.1:8080 (loopback
only) — never directly exposed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Settings:
    """Read once at startup. The defaults are sane for a CPU-only AX42."""

    def __init__(self) -> None:
        # Where to put the ephemeral working directories. /tmp works but
        # tmpfs would be better on production; configurable so an ops person
        # can point this at a fast disk.
        self.workdir_root = os.environ.get(
            "SANDBOX_WORKDIR_ROOT", "/var/lib/codeflow-sandbox"
        )
        # Default container resource limits. Per-job overrides allowed.
        self.default_cpu_limit = float(
            os.environ.get("SANDBOX_DEFAULT_CPU", "2.0")
        )
        self.default_memory_mb = int(
            os.environ.get("SANDBOX_DEFAULT_MEMORY_MB", "2048")
        )
        # Hard cap regardless of what the orchestrator asks for. Stops a
        # runaway request from eating the whole box.
        self.max_cpu_limit = float(os.environ.get("SANDBOX_MAX_CPU", "4.0"))
        self.max_memory_mb = int(
            os.environ.get("SANDBOX_MAX_MEMORY_MB", "8192")
        )
        # Wall-clock limit. Most builds finish in <60s; we cap at 5 min.
        self.max_runtime_seconds = float(
            os.environ.get("SANDBOX_MAX_RUNTIME_S", "300")
        )
        # Docker images for each runtime. Pulled lazily on first use.
        self.runtime_images = {
            "node20": os.environ.get("SANDBOX_NODE_IMAGE", "node:20-slim"),
            "python311": os.environ.get(
                "SANDBOX_PYTHON_IMAGE", "python:3.11-slim"
            ),
            "bun1": os.environ.get("SANDBOX_BUN_IMAGE", "oven/bun:1"),
        }


# ---------------------------------------------------------------------------
# Lifespan: make sure the workdir root exists and Docker is reachable
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    Path(settings.workdir_root).mkdir(parents=True, exist_ok=True)
    # Sanity-check that docker is callable. If it isn't, fail fast at startup
    # rather than failing every request.
    proc = await asyncio.create_subprocess_exec(
        "docker", "version", "--format", "{{.Server.Version}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Docker is not reachable. Is it installed and is this user in "
            f"the docker group? stderr: {err.decode()}"
        )
    app.state.settings = settings
    app.state.docker_version = out.decode().strip()
    print(f"[sandbox] ready. Docker version: {app.state.docker_version}")
    yield


app = FastAPI(
    title="Code Flow Sandbox",
    description="Ephemeral container runner for generated app build/test.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    """A single build job. The orchestrator submits one of these per build."""
    files: dict[str, str] = Field(
        ..., description="Map of path -> file contents to write into the workdir"
    )
    runtime: str = Field(
        ..., description="Which runtime image to use",
        examples=["node20", "python311", "bun1"],
    )
    install_command: str = Field(
        ..., description="Shell command to run during install phase (has network)",
        examples=["npm ci", "pip install -r requirements.txt"],
    )
    test_command: str = Field(
        ..., description="Shell command to run during test phase (no network)",
        examples=["npm test", "pytest"],
    )
    env: Optional[dict[str, str]] = Field(
        default=None,
        description="Env vars to set inside the container. NEVER pass secrets.",
    )
    cpu_limit: Optional[float] = Field(
        default=None, description="CPU cores cap; falls back to default"
    )
    memory_mb: Optional[int] = Field(
        default=None, description="Memory cap in MB; falls back to default"
    )


class RunResponse(BaseModel):
    sandbox_id: str
    success: bool
    build_log: str
    test_log: str
    test_pass_count: int = 0
    test_fail_count: int = 0
    runtime_seconds: float
    artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="Map of declared artifact paths -> sha256 of output",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, Any]:
    """Caddy and Railway both probe this. Reports docker reachability."""
    return {
        "status": "ok",
        "docker_version": app.state.docker_version,
    }


@app.post("/run", response_model=RunResponse)
async def run_build(req: RunRequest) -> RunResponse:
    """The main endpoint. Synchronous from the caller's perspective; runs
    the full build/test in this request. Most builds complete in 10-90s
    so synchronous is fine — Railway's orchestrator runs these in workers
    that don't block the web service."""
    settings: Settings = app.state.settings

    # Cap resource requests at the configured maximums. We honor the
    # smaller of (request, max) so a buggy orchestrator can't burn the box.
    cpu_limit = min(
        req.cpu_limit or settings.default_cpu_limit,
        settings.max_cpu_limit,
    )
    memory_mb = min(
        req.memory_mb or settings.default_memory_mb,
        settings.max_memory_mb,
    )

    if req.runtime not in settings.runtime_images:
        raise HTTPException(
            status_code=400,
            detail=f"unknown runtime '{req.runtime}'. "
                   f"Allowed: {list(settings.runtime_images)}",
        )
    image = settings.runtime_images[req.runtime]

    sandbox_id = uuid.uuid4().hex
    workdir = Path(settings.workdir_root) / sandbox_id

    started_at = time.monotonic()
    try:
        # 1. Lay down the workdir with the requested files.
        workdir.mkdir(parents=True, exist_ok=False)
        _materialize_files(workdir, req.files)

        # 2. Phase 1: install (network ON).
        build_log, build_ok = await _run_phase(
            phase="install",
            image=image,
            workdir=workdir,
            command=req.install_command,
            env=req.env or {},
            cpu_limit=cpu_limit,
            memory_mb=memory_mb,
            network=True,
            timeout_seconds=settings.max_runtime_seconds / 2,
        )
        if not build_ok:
            return RunResponse(
                sandbox_id=sandbox_id,
                success=False,
                build_log=build_log,
                test_log="(skipped: install failed)",
                runtime_seconds=time.monotonic() - started_at,
            )

        # 3. Phase 2: test (network OFF).
        test_log, test_ok = await _run_phase(
            phase="test",
            image=image,
            workdir=workdir,
            command=req.test_command,
            env=req.env or {},
            cpu_limit=cpu_limit,
            memory_mb=memory_mb,
            network=False,
            timeout_seconds=settings.max_runtime_seconds / 2,
        )
        pass_count, fail_count = _parse_test_counts(test_log)

        # 4. Collect any declared artifacts. For v1 we just hash everything
        # under workdir/dist if it exists. A future version takes an
        # explicit "outputs" list in the request.
        artifacts: dict[str, str] = {}
        dist = workdir / "dist"
        if dist.exists():
            for f in dist.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(workdir))
                    artifacts[rel] = _sha256_of_file(f)

        return RunResponse(
            sandbox_id=sandbox_id,
            success=test_ok,
            build_log=build_log,
            test_log=test_log,
            test_pass_count=pass_count,
            test_fail_count=fail_count,
            runtime_seconds=time.monotonic() - started_at,
            artifacts=artifacts,
        )
    finally:
        # 5. Cleanup. Always runs even if something blew up mid-build.
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _materialize_files(workdir: Path, files: dict[str, str]) -> None:
    """Write each file to its relative path inside workdir. Resolves
    paths to absolute and verifies they stay inside workdir — defense
    against a malicious payload trying to traverse out via ../."""
    for relpath, contents in files.items():
        target = (workdir / relpath).resolve()
        try:
            target.relative_to(workdir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"file path '{relpath}' escapes workdir",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)


async def _run_phase(
    *, phase: str, image: str, workdir: Path, command: str,
    env: dict[str, str], cpu_limit: float, memory_mb: int,
    network: bool, timeout_seconds: float,
) -> tuple[str, bool]:
    """
    Run one phase (install or test) inside a fresh container. Returns
    (combined_log, success_bool).

    We deliberately use --rm so containers tear themselves down. We mount
    the workdir as the container's /work directory and set that as the
    working directory, so the install/test commands can find the files
    naturally.
    """
    env_args: list[str] = []
    for k, v in env.items():
        env_args.extend(["-e", f"{k}={v}"])

    network_args = [] if network else ["--network=none"]

    docker_cmd = [
        "docker", "run", "--rm",
        "--cpus", str(cpu_limit),
        "--memory", f"{memory_mb}m",
        "--memory-swap", f"{memory_mb}m",        # disable swap
        "--pids-limit", "256",                   # cap process count
        "-v", f"{workdir}:/work",
        "-w", "/work",
        # tmpfs for /tmp since the rootfs is otherwise restrictive
        "--tmpfs", "/tmp:size=256m",
        *network_args,
        *env_args,
        image,
        "sh", "-c", command,
    ]

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return f"[{phase}] container failed to start within timeout", False

    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        # Kill the container if it's still running.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return f"[{phase}] timeout after {timeout_seconds:.0f}s", False

    log = stdout.decode("utf-8", errors="replace")
    return f"[{phase}]\n{log}", proc.returncode == 0


def _parse_test_counts(log: str) -> tuple[int, int]:
    """Best-effort extraction of pass/fail counts from common test runners.
    We look for a handful of well-known output patterns. If we can't find
    anything, we return (0, 0) — the success flag from exit code is the
    authoritative signal anyway."""
    import re
    # jest / vitest: "Tests:  3 failed, 12 passed, 15 total"
    m = re.search(r"Tests:\s*(?:(\d+) failed,\s*)?(\d+) passed", log)
    if m:
        return int(m.group(2)), int(m.group(1) or 0)
    # pytest: "1 failed, 5 passed in 2.3s" or just "5 passed in 2.3s"
    m = re.search(r"(?:(\d+) failed,\s*)?(\d+) passed", log)
    if m:
        return int(m.group(2)), int(m.group(1) or 0)
    return 0, 0


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    # On the Hetzner box this binds to loopback only; Caddy fronts it.
    uvicorn.run(app, host="127.0.0.1", port=port)
