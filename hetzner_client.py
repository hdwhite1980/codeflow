"""
codeflow.hetzner_client
=======================

Two thin clients for the Hetzner-hosted heavy-compute box.

  * OllamaClient — Tier 2 of the OPSIS escalation pattern. Cheap local LLM
    generation. Used for boilerplate files and routine audits where frontier
    quality isn't needed.

  * SandboxClient — Tier 4 of the pipeline. Spins up an isolated container,
    drops generated files in, runs install/build/test, returns the results.
    The sandbox runner is a small daemon you'll deploy to the same Hetzner
    box; spec lives in deploy/hetzner_sandbox.md.

Why a separate file for both
----------------------------
They share a host but nothing else. Splitting now means later we can move
the sandbox to a different machine (RunPod, Lambda, AWS Fargate) without
touching Ollama, and vice versa. Each client is configured by URL only.

Auth model
----------
Both endpoints sit on a Hetzner box reachable via Railway's outbound. We
use a shared bearer token (HETZNER_API_TOKEN env var) for both — simple,
rotatable, and the Hetzner box is the only thing holding the secret. NOT
mTLS in v1; revisit when we have a security audit.

TLS verification model
----------------------
The Hetzner box uses a self-signed certificate because we don't have a
public domain pointed at it (yet). Production httpx defaults to verifying
certs against the system CA bundle, which would reject self-signed certs.

We expose `verify_tls` on each client (default False) so callers can either:
  - Skip verification entirely (the default, fine for self-signed setups
    where the bearer token is the real auth)
  - Provide a path to a specific cert file to pin
  - Set verify=True once a real CA-signed cert is in place

The bearer token IS the authentication. TLS encrypts transport but does not
authenticate identity in this model. If you upgrade to a real domain + Let's
Encrypt later, set CODEFLOW_VERIFY_HETZNER_TLS=true in the orchestrator env.

Network surface
---------------
The Hetzner box should expose ONLY:
  * 443/tcp for the sandbox + ollama HTTPS reverse proxy (Caddy/Traefik)
  * 22/tcp for admin from a whitelisted IP
Nothing else. Ollama by default binds 0.0.0.0:11434 with no auth — that
must be tunneled through the reverse proxy with the bearer token enforced.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import httpx


# ---------------------------------------------------------------------------
# Shared TLS verification setting. Read from env once at import time so all
# clients in a single process share the same posture.
# ---------------------------------------------------------------------------

def _resolve_verify() -> Union[bool, str]:
    """
    Decide what to pass to httpx as the verify= parameter.

    Returns:
        False         -> skip TLS verification (default for self-signed)
        True          -> verify against system CA bundle (use with real cert)
        <path string> -> verify against the cert at this path (pinned)

    Controlled by CODEFLOW_VERIFY_HETZNER_TLS env var:
        unset or "false" -> False (skip)
        "true"           -> True (system CA)
        <any path>       -> that path (pinned)
    """
    raw = os.environ.get("CODEFLOW_VERIFY_HETZNER_TLS", "false").strip()
    if raw.lower() == "false" or raw == "":
        return False
    if raw.lower() == "true":
        return True
    # Anything else is treated as a path to a CA bundle / pinned cert file.
    return raw


# ---------------------------------------------------------------------------
# Ollama (Tier 2 local LLM)
# ---------------------------------------------------------------------------

@dataclass
class OllamaClient:
    """
    Calls a Hetzner-hosted Ollama instance. We use the /api/generate endpoint
    rather than /api/chat because v1 of the pipeline treats each generation
    request as stateless — context comes from the ledger, not from chat
    history. This is the right model for code generation; chat-style state
    creates subtle bugs when you parallelize over multiple AIs.
    """

    base_url: str                    # e.g. "https://ollama.hetzner.you.com"
    auth_token: str
    # Default to 7B because that's the model we ship on the default Hetzner
    # box. Override to qwen2.5-coder:32b if you have GPU hardware and pulled
    # the larger model.
    model: str = "qwen2.5-coder:7b"
    timeout_seconds: float = 120.0
    # Default False because the Hetzner box uses a self-signed cert.
    # Override via constructor or via CODEFLOW_VERIFY_HETZNER_TLS env var.
    verify_tls: Union[bool, str] = field(default_factory=_resolve_verify)

    async def generate(
        self, prompt: str, *, system: Optional[str] = None,
        temperature: float = 0.2, max_tokens: int = 4096,
    ) -> str:
        """Single-shot generation. Returns the text the model produced."""
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system:
            body["system"] = system

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, verify=self.verify_tls,
        ) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                headers=headers, json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")

    async def healthy(self) -> bool:
        """Used by the orchestrator to decide whether Tier 2 is available
        right now. If the Hetzner box is rebooting, we skip Tier 2 and let
        the request escalate to cloud APIs immediately."""
        try:
            async with httpx.AsyncClient(
                timeout=5.0, verify=self.verify_tls,
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/api/tags",
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
                return resp.status_code == 200
        except (httpx.HTTPError, httpx.TimeoutException):
            return False


# ---------------------------------------------------------------------------
# Sandbox runner (Tier 4 build/test)
# ---------------------------------------------------------------------------

@dataclass
class SandboxResult:
    """What the sandbox returns after running a build."""
    sandbox_id: str
    success: bool
    build_log: str
    test_log: str
    test_pass_count: int
    test_fail_count: int
    runtime_seconds: float
    artifacts: dict[str, str]        # file_path -> sha256 of output


@dataclass
class SandboxClient:
    """
    Spins up an ephemeral container on the Hetzner box, copies in files,
    runs the build, runs the tests, returns the verdict, and tears the
    container down. Implementation on the Hetzner side is a small FastAPI
    daemon that wraps `docker run --rm` (or podman, or firecracker — the
    HTTP contract here doesn't care).

    Why ephemeral matters
    ---------------------
    Generated code is hostile by default. A misaligned model can produce
    `rm -rf /` in a postinstall script. The sandbox runs each build in a
    fresh container with no network access except to npm/pypi mirrors, no
    persistent storage, and a hard wall-clock limit. The Hetzner box never
    executes generated code outside a sandbox.
    """

    base_url: str
    auth_token: str
    timeout_seconds: float = 600.0       # builds can be slow
    verify_tls: Union[bool, str] = field(default_factory=_resolve_verify)

    async def run_build(
        self,
        files: dict[str, str],            # path -> contents
        runtime: str,                     # 'node20' | 'python311' | 'bun1'
        install_command: str,
        test_command: str,
        *, env: Optional[dict[str, str]] = None,
        cpu_limit: float = 2.0,
        memory_mb: int = 2048,
    ) -> SandboxResult:
        """Submit a build job and wait for the result. The Hetzner daemon
        does the actual container lifecycle; we just orchestrate."""
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }
        body = {
            "files": files,
            "runtime": runtime,
            "install_command": install_command,
            "test_command": test_command,
            "env": env or {},
            "limits": {"cpu": cpu_limit, "memory_mb": memory_mb},
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, verify=self.verify_tls,
        ) as client:
            resp = await client.post(
                f"{self.base_url}/run", headers=headers, json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return SandboxResult(
                sandbox_id=data["sandbox_id"],
                success=data["success"],
                build_log=data.get("build_log", ""),
                test_log=data.get("test_log", ""),
                test_pass_count=data.get("test_pass_count", 0),
                test_fail_count=data.get("test_fail_count", 0),
                runtime_seconds=data.get("runtime_seconds", 0.0),
                artifacts=data.get("artifacts", {}),
            )

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=5.0, verify=self.verify_tls,
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/health",
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
                return resp.status_code == 200
        except (httpx.HTTPError, httpx.TimeoutException):
            return False


# ---------------------------------------------------------------------------
# Combined health check used by the orchestrator's /health endpoint
# ---------------------------------------------------------------------------

async def hetzner_health(
    ollama: OllamaClient, sandbox: SandboxClient,
) -> dict[str, bool]:
    """Run both checks in parallel so a single /health request stays fast."""
    ollama_ok, sandbox_ok = await asyncio.gather(
        ollama.healthy(), sandbox.healthy(),
    )
    return {"ollama": ollama_ok, "sandbox": sandbox_ok}
