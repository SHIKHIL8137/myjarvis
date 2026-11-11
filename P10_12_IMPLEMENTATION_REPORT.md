# P10.12 Implementation Report — Execution Runtime Hardening, PostgreSQL Target Protection & Agent Skills Manager

## 1. Mission & Inspection Baseline

- **Host Machine Inspected**: Windows 11, AMD Ryzen 5 5600H, NVIDIA GTX 1650 (4 GB VRAM), 15.3 GB RAM, Python `3.14.6` (`.venv`), Node `v24.16.0`, npm `11.13.0`.
- **OpenCode Runtime Inspected**: `opencode-ai@1.19.5` installed globally at `C:\Users\shikh\AppData\Roaming\npm\node_modules\opencode-ai` (`opencode --version` -> `1.19.5`); verified exact npm SRI digests (`opencode-ai@1.19.5`, `opencode-linux-x64@1.19.5`, `opencode-linux-arm64@1.19.5`) via `npm view`.
- **Docker & PostgreSQL Status Inspected**: Docker CLI `29.6.1` installed, but Windows service `com.docker.service` is `Stopped` (`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`). `postgresql-x64-18` service is `Running`, with `JARVIS_PG_TEST_URL` and `JARVIS_PG_SCRATCH_URL` unconfigured in environment.

---

## 2. Deliverables Implemented

### 2.1 P10.12.1 — Production-Ready OpenCode Docker Runtime
- **`deploy/opencode.Dockerfile`**: Pinned `node:22-bookworm-slim` image with minimal packages (`git`, `ca-certificates`, `python3`, `python3-venv`), exact `OPENCODE_VERSION=1.19.5`, SHA-512 SRI integrity check executed in Node `crypto.createHash('sha512')` on downloaded npm tarballs prior to extraction, non-root `jarvis` UID/GID `10001:10001`, `WORKDIR /workspace`, `HEALTHCHECK` (`opencode --version`), and zero host `.env` / `$HOME` / Docker socket mounts.
- **`deploy/docker-compose.opencode.yml` & `deploy/README.md`**: Reference compose profile enforcing `read_only: true`, `--cap-drop=ALL`, `--security-opt=no-new-privileges:true`, `--pids-limit=256`, ephemeral `tmpfs` scratch mounts, and documented Windows Docker Desktop path rules.
- **`jarvis/gateway/sandbox_proxy.py`**:
  - `ScopedGatewayTokenManager`: issues revocable single-run tokens (`jgw_...`) scoped to `(run_id, allowed_models, allow_ollama, expires_at)` and records non-prompt/non-secret telemetry.
  - `RestrictedEgressProxy`: loopback HTTP/CONNECT proxy enforcing `allowed_egress_domains` + `netsec.resolve_and_validate` / `audit.is_public_ip` SSRF blocking and explicit Ollama gating (`allow_ollama=False` by default).
- **`jarvis/integrations/git_worktree.py` & `opencode_adapter.py` & `api.py` & `api_v2.py`**:
  - Switched `WorkforceSpec.docker_image` default to `"jarvis-opencode:1.19.5"`; added `to_docker_mount_path()` for Windows Docker Desktop backslash -> forward-slash conversion; implemented `ExecutionPolicy.validate_network_policy()` supporting `"offline"`, `"model_gateway"`, `"restricted_egress"`, and owner-authorized `"unrestricted"` with fail-closed validation; added `/api/v2/sandbox/v1/models` and `/api/v2/sandbox/v1/chat/completions` scoped bearer endpoints.

### 2.2 P10.12.2 — Central Database Target Registry & Production Restore Protection
- **`jarvis/db/targets.py`**: `DatabaseTargetRegistry` and `normalize_database_identity()` normalize SQLite and PostgreSQL targets into `driver://host:port/database` fingerprints without storing plaintext passwords, auto-ingesting active DB, config `production_targets` / `scratch_target_url` / `test_target_url` / `registered_targets`, and environment variables (`JARVIS_PROD_DATABASE_URL`, `JARVIS_SCRATCH_DATABASE_URL`, `JARVIS_PG_TEST_URL`, `JARVIS_PG_SCRATCH_URL`, etc.).
- **`jarvis/recovery/backup.py` & `jarvis/agents/approvals.py` & `api_v2.py`**:
  - Protected `restore_postgresql()` even when Jarvis runs local SQLite against a remote production PG URL. Rejects `unknown`, `ambiguous`, and `prod_pg`/`is_current_live` targets unless a single-use `ApprovalRequestRow` (`restore_production_database`) matching normalized `target_fingerprint` + `dump_sha256` is approved and consumed (`approvals.consume_approved`), auditing decisions to `AuditEventRow` without secrets.
  - Enforces `verify_backup(..., scratch_pg_url=...)` strictly to isolated registered `scratch_pg` targets (`403` on `/api/v2/recovery/verify` for non-scratch targets; no boolean override flags on `/verify`).
  - Added `/api/v2/recovery/targets`, `/api/v2/recovery/restore/request-production`, and `/api/v2/recovery/restore/execute`.

### 2.3 P10.12.3 & P10.12.4 — Provider-Independent Agent Skills & Capabilities Manager + 7-Repo Audit
- **Database Schema & Migration**: Added `SkillRow`, `SkillVersionRow`, `SkillAssignmentRow`, and `SkillMetricRow` in `jarvis/db/models.py` and reversible Alembic migration `jarvis/db/migrations/versions/0003_p10_12_skills_and_targets.py`.
- **`jarvis/skills/` Modules**:
  - `metadata.py`: Canonical 20-field `SkillManifest`, `normalize_skill_id()`, safe frontmatter parser, and deterministic `compute_directory_sha256()`.
  - `security.py`: Scans Markdown docs, scripts, hooks, and MCP configs without execution (`remote_shell_pipe`, `runtime_binary_download`, `policy_override_attempt`, `privilege_escalation`, `credential_harvesting`, `dynamic_code_exec`, embedded secrets), enforces script quarantine when `allow_untrusted_scripts=False`, and wraps instructions with `enforce_system_policy_precedence()` (`<jarvis_skill policy_precedence="jarvis_system_first">`).
  - `compatibility.py`: Explicit compatibility evaluation across `jarvis_native`, `opencode` (`.opencode/skills/<id>/SKILL.md`), `claude_code` (`skill_md` vs `plugin_with_hooks_or_mcp`), and `codex` (`.agents/skills/<id>/SKILL.md`), raising `SkillCompatibilityError` with exact reasons on incompatibility rather than assuming Markdown implies universal support.
  - `validator.py` & `discovery.py`: Validates manifests, licenses (`allowed_licenses`), checksums, and security BEFORE owner approval; discovers local/project skills read-only.
  - `registry.py`: Persists skill metadata in SQLite/PostgreSQL and isolated files in `data/skills/installed/<skill_id>/<version_tag>/`, enforcing duplicate rejection, approval gates, update history, integrity-verified rollback, enable/disable/uninstall, role/project assignments, and metrics with empirical `qa_improvement_delta` (`degrading_recommend_disable` detection).
  - `router.py` & Orchestrator Wiring: `AgentCapabilityRouter` sits between `DecisionEngine` and `workforce_graph.py` / `assistant_graph.py`, skips trivial/casual prompts, matches task categories (`security_audit`, `testing_api`, `code_review`, `debugging`, `architecture`, `refactoring`, `react_frontend`, `ui_review`, `technical_explanation`), combines compatible skills within `max_context_chars`, materializes `.opencode/skills/` selectively for OpenCode runs, and records invocation metrics.
- **Audited 7 External Skill Sources (`jarvis/skills/recommended_audit.py`)**:

| Repository | Inspected Commit | License | Verdict & Jarvis Notes |
|---|---|---|---|
| `cloudflare/security-audit-skill` | `c1c8a8c14710` | MIT | **approve_recommended** (`22` files): Agent-neutral `skills/security-audit/SKILL.md` + zero-dep `.cjs` validators; OpenCode compatible (`instruction_only` validated). |
| `mattpocock/skills` | `4ae2317ddb22` | MIT | **approve_selective** (`184` files, `40` `SKILL.md`): Prioritize `tdd`, `code-review`, `diagnosing-bugs`, `domain-modeling`; skip `git-guardrails-claude-code` & global symlink script. |
| `DietrichGebert/ponytail` | `96608e078b2d` | MIT | **approve_selective** (`216` files): Extract `skills/ponytail/SKILL.md` minimal-scope discipline; exclude JS/PS1/SH harness hooks. |
| `pbakaus/impeccable` | `c1e0e5492fbb` | Apache-2.0 | **approve_guidance_only_quarantined** (`4,527` files): `SKILL.md` directs running `${CLAUDE_SKILL_DIR}/scripts/impeccable` (self-downloading binary launcher!); incompatible OpenCode verbatim; extract static craft rules only if approved. |
| `affaan-m/everything-claude-code` | `d2ef2680594f` | MIT | **do_not_install_framework** (`4,701` files): Massive harness with global `install.sh`/`install.ps1`, `ecc-memory-mcp`, control-plane & hooks conflicting with Jarvis OS. |
| `wheresryan22/anatomy` | `1482e8469b0a` | MIT | **optional_documentation** (`173` files): Needs Node 20.10+ & Chrome/Chromium for SVG/WebGL checks + optional gallery publish; guidance mode works natively. |
| `nutlope/hallmark` | `04977e15f3df` | MIT | **approve_selective** (`860` files): Safe CSS/a11y floor rules in `skills/hallmark/SKILL.md`; quarantine `install-hook.mjs`, Playwright `capture.mjs`, and `imagegen.mjs`. |

### 2.4 P10.12.5 — React Dashboard & Modular APIs
- **`jarvis/api_skills.py`**: Exposes `/api/v2/skills/*` (`GET /skills`, `/catalog/{source_id}`, `/discover`, `/validate`, `/install/request`, `/install`, `/enable`, `/disable`, `/update`, `/rollback`, `DELETE`, `/assign`, `/unassign`, `/history`, `/select-preview`).
- **`apps/frontend/src/features/SkillsPanel.tsx`, `App.tsx`, `RecoveryPanel.tsx`**: Added `SKILLS` tab (installed skills, permissions, runtime compat, assignments, metrics, version history/rollback, router preview, audited 7-source catalog, explicit `window.confirm` checks, no executable script controls) and Database Target Registry display in `RECOVERY`.

---

## 3. Honest Verification Classification Matrix

| Area | Implemented | Unit Tested | Integration Tested | Live Verified | Not Verified / Not Implemented |
|---|---|---|---|---|---|
| OpenCode Dockerfile & CLI pinning (`1.19.5` + SRI hashes) | Yes | File/invariant check (`test_p10_12_1`) | No live container build | Host `opencode 1.19.5` & npm SRI checked | **Not verified live**: `docker build`/`docker run` (`com.docker.service` stopped; live Docker test cleanly skipped) |
| Network policies, `ScopedGatewayTokenManager` & `RestrictedEgressProxy` | Yes | Yes (HTTP/CONNECT proxy SSRF/domain block, auth fail-closed, no secret/prompt log leak) | Yes (`/api/v2/sandbox/v1/chat/completions` via `TestClient`) | Proxy localhost loop verified live | Real container -> host bridge call **Not verified live** (Docker stopped) |
| Windows mount normalization & container security flags | Yes | Yes (`to_docker_mount_path`, `--cap-drop=ALL`, `--user 10001:10001`, `--read-only`, `--pids-limit`) | Command assembly verified | N/A | Real Windows Docker Desktop bind mount I/O **Not verified live** |
| Central DB target registry & SQLite->PG prod restore protection | Yes | Yes (`DatabaseTargetRegistry`, normalization, password redaction) | Yes (`TestClient` `/request-production` -> `/approve` -> `/execute` single-use + `/verify` 403 prod block, mocked `pg_restore`) | `/api/v2/recovery/targets` live HTTP `200` | Live `pg_restore` against external PostgreSQL server **Not verified live** (`JARVIS_PG_TEST_URL`/`JARVIS_PG_SCRATCH_URL` unset) |
| Agent Skills Manager (`jarvis/skills/*`), router, metrics & 7-source audit | Yes | Yes (checksum tamper, license, malicious script/override, dupes, update/rollback, compat adapters, router budget) | Yes (`/api/v2/skills/*` + Orchestrator & React Vite dist check + smoke check on real cloned 7 repo dirs) | `/api/v2/skills` live HTTP `200` (`catalog=7`) | Automatic one-click remote `git clone` installer inside UI intentionally **Not implemented** (requires owner validation/local review policy); live React browser click-through **Not verified live** (Chrome remote debugging inactive) |

