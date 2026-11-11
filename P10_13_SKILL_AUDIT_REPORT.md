# P10.13 Phase 1 — Seven-Skill Security, License & Runtime Audit Report

Verified by fresh read-only shallow clones (`git -c core.hooksPath=NUL clone --depth 1`, `2026-11-11`) without executing any repository scripts, installers, or hooks on the host.

## 1. Strict Six-State Audit Taxonomy & Separated Lifecycle Policy

Every repository and sub-skill is classified strictly under:
- `approved` — clean pure Markdown / instruction-only skill compatible across target runtimes with zero critical or script findings.
- `approved_with_restrictions` — selective Markdown extraction approved ONLY after excluding/quarantining scripts, hooks, MCP configs, or browser launchers.
- `needs_manual_review` — requires explicit owner inspection/sanitization due to native binary launchers, external runtime requirements (`Node 20+` + Chromium), or network publishing assumptions.
- `blocked` — tripped critical security checks (e.g., hardcoded secret/credential patterns or policy override attempts).
- `incompatible` — cannot execute on the requested runtime (e.g., `${CLAUDE_SKILL_DIR}` / headless Chromium missing from `jarvis-opencode:1.19.5`).
- `do_not_install` — external agent harness framework conflicting directly with Jarvis OS governance, memory, and safety gates.

Jarvis separates lifecycle states in `/api/v2/skills` and the React `SKILLS` panel across six orthogonal flags:
`audited` -> `approved` -> `installed` -> `enabled` -> `assigned` -> `successfully_executed`.

---

## 2. Reconciled Audit Matrix (All 7 Upstream Repositories)

| Source Repository | Inspected Commit SHA (Date) | License | Tracked Files (`SKILL.md` / Scripts) | P10.13 Classification (`policy_verdict`) | Runtime Compatibility Summary | Security & Policy Notes |
|---|---|---|---|---|---|---|
| [`cloudflare/security-audit-skill`](https://github.com/cloudflare/security-audit-skill) | `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8` (`2026-09-14`) | MIT | `22` files (`1` `SKILL.md`, `4` `.cjs`) | **`approved_with_restrictions`** | `jarvis_native`, `opencode`, `claude_code`, `codex` (all compatible in `instruction_only`) | 0 hooks, 0 MCP, 0 downloaders. Educational regex hits (`ATTACK-CLASSES.md`, example `Bearer` docs) + 4 `.cjs` validators quarantined by default (`allow_untrusted_scripts=false`). |
| [`mattpocock/skills`](https://github.com/mattpocock/skills) | `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d` (`2026-10-31`) | MIT | `184` files (`39` `SKILL.md`*, `7` scripts) | **`approved_with_restrictions`** | Selected subpaths (`tdd`, `code-review`, `domain-modeling`) **`approved`** across all 4 runtimes; `diagnosing-bugs` OpenCode-incompatible verbatim (`localhost` Chromium ref) | *Note: `P10_12_IMPLEMENTATION_REPORT.md` cited `40` `SKILL.md` files; actual count verified `39`. Exclude `scripts/link-skills.sh` and `git-guardrails-claude-code` (`do_not_install`). |
| [`DietrichGebert/ponytail`](https://github.com/DietrichGebert/ponytail) | `96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (`2026-10-29`) | MIT | `216` files (`12` `SKILL.md`, `60` scripts) | **`approved_with_restrictions`** | Extracted `skills/ponytail` & `skills/ponytail-review` **`approved`** across all 4 runtimes | Extracted subpaths had 0 findings/scripts/hooks; repo-wide `.opencode/plugins/`, `hooks/*.js/*.sh/*.ps1`, and IDE plugin manifests are excluded. |
| [`pbakaus/impeccable`](https://github.com/pbakaus/impeccable) | `c1e0e5492fbb524f5df84d635ecfae7dc438ec45` (`2026-11-10`) | Apache-2.0 | `4,527` files (`26` `SKILL.md`, `413` scripts) | **`needs_manual_review`** | `claude_code` compatible; **`opencode` & `codex` incompatible** verbatim | `plugin/skills/impeccable/SKILL.md` L19 invokes `${CLAUDE_SKILL_DIR}/scripts/impeccable` which downloads a native platform binary from GitHub releases (`impeccable/releases/download/engine-v...`) into `~/.impeccable/bin`; also ships `live-browser.js` / `modern-screenshot.umd.js` (`eval`/`exec` warning) and `>1 MB` `font-index.json`. Only manually sanitized design-floor Markdown allowed if audited. |
| [`affaan-m/everything-claude-code`](https://github.com/affaan-m/everything-claude-code) | `d2ef2680594f9a17526a6e1a2e18b3d242c5d17b` (`2026-11-06`) | MIT | `4,701` files (`1,068` `SKILL.md`, `1,298` scripts) | **`do_not_install`** | Incompatible competing multi-agent harness | Ships `hooks/hooks.json` intercepting `Bash`/`PowerShell`/`Write`/`Edit`, `.mcp.json` launching `npx -y chrome-devtools-mcp@1.10.1`, custom memory MCP, and payment/OSINT modules. Direct subpath scan verified `skills/security-review` tripped **`blocked`** (`critical_count=2` hardcoded sample password/API key literals). |
| [`wheresryan22/anatomy`](https://github.com/wheresryan22/anatomy) | `1482e8469b0a9e0d8cc18ea2740a01423d29ae80` (`2026-10-18`) | MIT | `173` files (`1` `SKILL.md`, `98` scripts) | **`needs_manual_review`** | `jarvis_native` (guidance), `claude_code`, `codex`; **`opencode` incompatible** verbatim | 0 hooks/MCP, but bundles 98 `.mjs`/`.js` kit/render scripts (`Node 20.10+` + headless Chrome/Chromium) and optional upload instructions (`Anatomy community gallery` blocked under Local Mode). |
| [`nutlope/hallmark`](https://github.com/nutlope/hallmark) | `04977e15f3dfd6ee4b74c887dac3eb52665d6c92` (`2026-11-11`) | MIT | `860` files (`1` `SKILL.md`, `50` scripts) | **`approved_with_restrictions`** | `jarvis_native` / `codex` / `claude_code` compatible in `instruction_only`; **`opencode` incompatible** verbatim | Root `SKILL.md` ("The Floor": 16 CSS/a11y rules) is clean guidance when scripts are stripped; bundled `install-hook.mjs` mutates `.claude/settings.json` `PostToolUse`, `imagegen.mjs` calls external API (`TOGETHER_API_KEY`), `capture.mjs` requires Chromium, and 6 `.mjs` files match `dynamic_code_exec` warnings. |

---

## 3. Lifecycle State Verification Against Live Jarvis Registry

- In a clean default installation (`data/jarvis.db`), all 7 repositories start with `audited=True`, `approved=False`, `installed=False`, `enabled=False`, `assigned=False`, `successfully_executed=False`.
- Owner validation + approval + install + assignment + workflow execution updates each boolean independently in `/api/v2/skills`, surfaced separately in [`SkillsPanel.tsx`](file:///d:/projects/myjarvis/apps/frontend/src/features/SkillsPanel.tsx).
- `assistant_graph.py` & `orchestrator.start_assistant_workflow()` discrepancy resolved: selected assistant skills now propagate `[skills=<id>]` summary provenance and record `SkillMetricRow` invocations.

