# Jarvis Containerized Runtime & Database Deployment

## 1. OpenCode Sandbox Image (`jarvis-opencode:1.19.5`)

Build from the repository root with SHA-512 integrity verification against pinned npm tarballs (`opencode-ai@1.19.5`, `opencode-linux-x64@1.19.5`, `opencode-linux-arm64@1.19.5`):

```powershell
docker build -f deploy/opencode.Dockerfile -t jarvis-opencode:1.19.5 .
docker run --rm jarvis-opencode:1.19.5 opencode --version
```

### Container Security Guarantees
- Non-root UID/GID `10001:10001` (`USER jarvis`).
- Root filesystem mounted `--read-only` with small ephemeral `--tmpfs` scratch mounts (`/tmp`, `/home/jarvis/.local/state`).
- Only the single task Git worktree is mounted read/write at `/workspace:rw`.
- Host home directory, `.env`, SSH keys, provider API keys, and `/var/run/docker.sock` are NEVER mounted.
- `--cap-drop=ALL`, `--security-opt=no-new-privileges:true`, `--pids-limit=256`, CPU (`--cpus`), and memory (`--memory`) limits enforced.

### Configurable Network Policies (`workforce.network_policy`)
1. `offline` (default): `--network=none`. Zero outbound network access.
2. `model_gateway`: Routes agent model traffic ONLY to Jarvis's scoped OpenAI-compatible proxy endpoint using single-run short-lived bearer tokens (`JARVIS_GATEWAY_TOKEN`). Real provider API keys stay inside the host Jarvis process.
3. `restricted_egress`: Routes outbound HTTP(S) through Jarvis's SSRF-guarded domain-allowlist proxy (`HTTP_PROXY`/`HTTPS_PROXY`) constrained to `workforce.allowed_egress_domains` (and optional Ollama ONLY when `allow_ollama_in_sandbox=true`).
4. `unrestricted`: Refused unless `docker_allow_unrestricted_network=true` AND an explicit owner authorization actor is recorded.

### Windows Docker Desktop Notes
- Docker Desktop must have the Linux engine pipe running (`npipe:////./pipe/dockerDesktopLinuxEngine`).
- If Docker Desktop is stopped and `workforce.allow_host_fallback=false` (default), `opencode_adapter` refuses to run unisolated on the host.
- Worktree paths are normalized safely via `to_docker_mount_path()` without exposing host paths outside the target worktree.

## 2. PostgreSQL + `pgvector` Stack

```powershell
docker compose -f deploy/docker-compose.db.yml up -d
```

See root [README.md](../README.md) for Alembic migrations and production restore protection configuration.

