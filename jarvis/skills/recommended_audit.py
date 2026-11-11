"""Individual security, license, & compatibility audits for the 7 curated upstream skill repositories.

Every commit SHA, license, tracked file count, script path, hook/MCP inventory, and runtime constraint below was verified
by read-only upstream inspection (2026-11-11). Never executes untrusted scripts and never installs automatically.

Strict P10.13 Audit Taxonomy:
  - approved
  - approved_with_restrictions
  - needs_manual_review
  - blocked
  - incompatible
  - do_not_install
"""

SKILL_AUDIT_CLASSIFICATIONS = (
    "approved",
    "approved_with_restrictions",
    "needs_manual_review",
    "blocked",
    "incompatible",
    "do_not_install",
)

RECOMMENDED_SKILL_AUDIT_CATALOG: list[dict] = [
    {
        "source_id": "cloudflare-security-audit",
        "repo": "https://github.com/cloudflare/security-audit-skill",
        "pinned_commit": "c1c8a8c1471069fb0e188eeaff69b8e8db6564a8",
        "commit_date": "2026-09-14",
        "license": "mit",
        "tracked_files": 22,
        "skill_md_count": 1,
        "script_count": 4,
        "individual_skills": [
            {
                "skill_id": "cloudflare-security-audit",
                "subpath": "skills/security-audit",
                "name": "security-audit",
                "category": "security_audit",
                "classification": "approved_with_restrictions",
                "recommended_roles": ["qa_agent", "coder_agent", "architect_agent"],
            }
        ],
        "supported_runtimes": ["jarvis_native", "opencode", "claude_code", "codex"],
        "dependencies": ["none for instruction_only; Node.js optional for quarantined .cjs validators"],
        "scripts_found": [
            "skills/security-audit/validate-coverage-ledger.cjs",
            "skills/security-audit/validate-coverage-ledger.test.cjs",
            "skills/security-audit/validate-findings.cjs",
            "skills/security-audit/validate-findings.test.cjs",
        ],
        "hooks_or_mcp": [],
        "external_commands": ["node validate-coverage-ledger.cjs", "node validate-findings.cjs"],
        "install_instructions_summary": "Extract skills/security-audit/ with explicit owner approval; run instruction_only by default with .cjs validators quarantined.",
        "security_findings": [
            "MIT licensed, 0 hooks, 0 MCP configs, 0 remote curl|sh installers, explicit Guidance vs Full-Audit consent gate.",
            "Static scan flagged 4 warnings: educational attack patterns in ATTACK-CLASSES.md, example Bearer strings in protocol docs, and 4 bundled .cjs validators quarantined unless sandbox-approved.",
        ],
        "jarvis_compatibility": "HIGH — Agent-neutral Markdown workflow runs cleanly across jarvis_native, OpenCode (.opencode/skills/), Claude Code, and Codex with system-policy precedence.",
        "recommendation": "RECOMMENDED (Priority 1, Restricted) — Install skills/security-audit in instruction_only mode (scripts quarantined) after explicit owner approval.",
        "policy_verdict": "approved_with_restrictions",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
    {
        "source_id": "mattpocock-skills",
        "repo": "https://github.com/mattpocock/skills",
        "pinned_commit": "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d",
        "commit_date": "2026-10-31",
        "license": "mit",
        "tracked_files": 184,
        "skill_md_count": 39,
        "script_count": 7,
        "individual_skills": [
            {"skill_id": "mattpocock-tdd", "subpath": "skills/engineering/tdd", "name": "tdd", "category": "testing_api", "classification": "approved", "recommended_roles": ["coder_agent", "qa_agent"]},
            {"skill_id": "mattpocock-code-review", "subpath": "skills/engineering/code-review", "name": "code-review", "category": "code_review", "classification": "approved", "recommended_roles": ["qa_agent", "coder_agent"]},
            {"skill_id": "mattpocock-domain-modeling", "subpath": "skills/engineering/domain-modeling", "name": "domain-modeling", "category": "architecture", "classification": "approved", "recommended_roles": ["architect_agent"]},
            {"skill_id": "mattpocock-diagnosing-bugs", "subpath": "skills/engineering/diagnosing-bugs", "name": "diagnosing-bugs", "category": "debugging", "classification": "approved_with_restrictions", "recommended_roles": ["coder_agent", "qa_agent"]},
            {"skill_id": "git-guardrails-claude-code", "subpath": "skills/misc/git-guardrails-claude-code", "name": "git-guardrails-claude-code", "category": "security_audit", "classification": "do_not_install", "recommended_roles": []},
        ],
        "supported_runtimes": ["jarvis_native", "opencode", "claude_code", "codex"],
        "dependencies": ["none for tdd/code-review/domain-modeling; diagnosing-bugs shell script references browser checks"],
        "scripts_found": [
            "scripts/link-skills.sh",
            "scripts/list-skills.sh",
            "scripts/sync-plugin-version.mjs",
            "skills/engineering/diagnosing-bugs/scripts/hitl-loop.template.sh",
            "skills/misc/git-guardrails-claude-code/scripts/block-dangerous-git.sh (do_not_install)",
        ],
        "hooks_or_mcp": [".claude-plugin/marketplace.json", ".claude-plugin/plugin.json"],
        "external_commands": ["scripts/link-skills.sh (global symlink installer — EXCLUDE)"],
        "install_instructions_summary": "Extract ONLY verified subpaths (tdd, code-review, domain-modeling) individually; exclude link-skills.sh and git-guardrails-claude-code.",
        "security_findings": [
            "Verified 39 SKILL.md skills under MIT license. Core engineering skills (tdd, code-review, domain-modeling) passed scan with 0 findings across all 4 runtimes.",
            "diagnosing-bugs ships hitl-loop.template.sh referencing localhost browser automation (quarantined; incompatible with headless offline OpenCode verbatim).",
            "Repo-wide scripts/link-skills.sh & git-guardrails-claude-code MUST NOT be installed because Jarvis enforces isolated worktree Git gates natively.",
        ],
        "jarvis_compatibility": "HIGH (Selective Extraction) — Selected Markdown engineering subpaths are clean across Jarvis, OpenCode, Claude Code, and Codex; repo-wide plugin scripts blocked.",
        "recommendation": "RECOMMENDED (Priority 1, Selective Restrictions) — Approve individual pure-Markdown skills (`tdd`, `code-review`, `domain-modeling`); block global symlink/git-hook scripts.",
        "policy_verdict": "approved_with_restrictions",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
    {
        "source_id": "dietrichgebert-ponytail",
        "repo": "https://github.com/DietrichGebert/ponytail",
        "pinned_commit": "96608e078b2d5ea607c5c3b8b5a54d1c1fba6846",
        "commit_date": "2026-10-29",
        "license": "mit",
        "tracked_files": 216,
        "skill_md_count": 12,
        "script_count": 60,
        "individual_skills": [
            {"skill_id": "ponytail", "subpath": "skills/ponytail", "name": "ponytail", "category": "refactoring", "classification": "approved", "recommended_roles": ["coder_agent"]},
            {"skill_id": "ponytail-review", "subpath": "skills/ponytail-review", "name": "ponytail-review", "category": "refactoring", "classification": "approved", "recommended_roles": ["qa_agent", "coder_agent"]},
        ],
        "supported_runtimes": ["jarvis_native", "opencode", "claude_code", "codex"],
        "dependencies": ["none for extracted skills/ponytail SKILL.md; hooks & benchmarks excluded"],
        "scripts_found": [
            ".opencode/plugins/index.js",
            ".opencode/plugins/ponytail.mjs",
            "hooks/ponytail-activate.js",
            "hooks/ponytail-runtime.js",
            "hooks/ponytail-statusline.sh",
            "hooks/ponytail-statusline.ps1",
        ],
        "hooks_or_mcp": ["plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "hooks/claude-codex-hooks.json"],
        "external_commands": ["IDE/CLI session hook plugins across Claude/Codex/OpenCode (EXCLUDED)"],
        "install_instructions_summary": "Extract ONLY skills/ponytail and skills/ponytail-review Markdown folders; exclude .opencode/plugins and hooks/.",
        "security_findings": [
            "MIT licensed. Extracted subpaths skills/ponytail and skills/ponytail-review passed scan with 0 findings, 0 scripts, 0 hooks.",
            "Repository ships 60 script/plugin files (.opencode/plugins/, hooks/, benchmarks/) that hijack session hooks if installed wholesale; strictly excluded.",
        ],
        "jarvis_compatibility": "HIGH (Selective Guidance) — Clean minimal-diff guidance in Jarvis, OpenCode, Claude Code, and Codex when extracted without hooks.",
        "recommendation": "RECOMMENDED (Priority 1, Restricted Extraction) — Install extracted `skills/ponytail` / `skills/ponytail-review` instruction_only without harness hooks.",
        "policy_verdict": "approved_with_restrictions",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
    {
        "source_id": "pbakaus-impeccable",
        "repo": "https://github.com/pbakaus/impeccable",
        "pinned_commit": "c1e0e5492fbb524f5df84d635ecfae7dc438ec45",
        "commit_date": "2026-11-10",
        "license": "apache-2.0",
        "tracked_files": 4527,
        "skill_md_count": 26,
        "script_count": 413,
        "individual_skills": [
            {"skill_id": "impeccable", "subpath": "plugin/skills/impeccable", "name": "impeccable", "category": "react_frontend", "classification": "needs_manual_review", "recommended_roles": ["coder_agent"]},
        ],
        "supported_runtimes": ["claude_code", "jarvis_native (manual sanitized docs only)"],
        "dependencies": ["Self-downloading platform binary via ${CLAUDE_SKILL_DIR}/scripts/impeccable"],
        "scripts_found": [
            "plugin/skills/impeccable/scripts/impeccable",
            "plugin/skills/impeccable/scripts/impeccable.cmd",
            "plugin/skills/impeccable/scripts/live-browser.js",
            "plugin/skills/impeccable/scripts/modern-screenshot.umd.js",
            "plugin/skills/impeccable/scripts/data/font-index.json (>1MB)",
        ],
        "hooks_or_mcp": [".claude-plugin/plugin.json", ".codex/hooks.json", "plugin/hooks/hooks.json"],
        "external_commands": ["`${CLAUDE_SKILL_DIR}/scripts/impeccable context` (downloads engine binary from GitHub releases on first run)"],
        "install_instructions_summary": "DO NOT install verbatim package or scripts/; owner must manually inspect and copy static reference/craft-floor Markdown only.",
        "security_findings": [
            "HIGH RISK VERBATIM: plugin/skills/impeccable/SKILL.md directs execution of `${CLAUDE_SKILL_DIR}/scripts/impeccable` which downloads a native platform binary from GitHub releases into ~/.impeccable/bin on first run.",
            "Ships 413 scripts, live-browser.js & modern-screenshot.umd.js dynamic_code_exec findings, and >1 MB font-index.json; INCOMPATIBLE with OpenCode & Codex verbatim.",
        ],
        "jarvis_compatibility": "INCOMPATIBLE VERBATIM — OpenCode and Codex compatibility fail due to `${CLAUDE_SKILL_DIR}` binary launcher and Chromium assumptions.",
        "recommendation": "NEEDS MANUAL REVIEW — Reject verbatim install; allow only manually sanitized static design-floor Markdown if explicitly audited by owner.",
        "policy_verdict": "needs_manual_review",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
    {
        "source_id": "affaan-m-everything-claude-code",
        "repo": "https://github.com/affaan-m/everything-claude-code",
        "pinned_commit": "d2ef2680594f9a17526a6e1a2e18b3d242c5d17b",
        "commit_date": "2026-11-06",
        "license": "mit",
        "tracked_files": 4701,
        "skill_md_count": 1068,
        "script_count": 1298,
        "individual_skills": [
            {"skill_id": "ecc-security-review", "subpath": "skills/security-review", "name": "security-review", "category": "security_audit", "classification": "blocked", "recommended_roles": []},
            {"skill_id": "ecc-agent-security-hardening", "subpath": "skills/agent-security-hardening", "name": "agent-security-hardening", "category": "security_audit", "classification": "needs_manual_review", "recommended_roles": []},
        ],
        "supported_runtimes": ["claude_code (external competing harness)"],
        "dependencies": ["Node.js", "ecc-universal CLI", "external MCP servers (.mcp.json)", "multi-language harnesses"],
        "scripts_found": ["1,298 total scripts including .claude/, .cursor/hooks/, .codebuddy/, scripts/hooks/plugin-hook-bootstrap.js"],
        "hooks_or_mcp": [".mcp.json (npx -y chrome-devtools-mcp@1.10.1)", "hooks/hooks.json (PreToolUse inline Node interceptors)", "ecc-memory-mcp"],
        "external_commands": ["npx -y chrome-devtools-mcp@1.10.1", "PreToolUse shell dispatchers", "global installers"],
        "install_instructions_summary": "DO NOT INSTALL framework. Competing agent OS harness conflicts directly with Jarvis core.",
        "security_findings": [
            "FRAMEWORK CONFLICT & RISK: Ships 4,701 files (1,068 SKILL.md, 1,298 scripts), PreToolUse Bash/PowerShell/Write intercept hooks, external npx MCP bootstrapping, and custom memory/governance layers.",
            "Real scan verified: `skills/security-review` tripped critical `embedded_secret` checks (literal sample keys in SKILL.md & cloud-infrastructure-security.md -> blocked).",
        ],
        "jarvis_compatibility": "INCOMPATIBLE HARNESS — Directly duplicates and conflicts with Jarvis Orchestrator, 5-way memory, DB target guards, and LangGraph approvals.",
        "recommendation": "DO NOT INSTALL — Framework rejected wholesale; keep uninstalled.",
        "policy_verdict": "do_not_install",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
    {
        "source_id": "wheresryan22-anatomy",
        "repo": "https://github.com/wheresryan22/anatomy",
        "pinned_commit": "1482e8469b0a9e0d8cc18ea2740a01423d29ae80",
        "commit_date": "2026-10-18",
        "license": "mit",
        "tracked_files": 173,
        "skill_md_count": 1,
        "script_count": 98,
        "individual_skills": [
            {"skill_id": "anatomy", "subpath": ".", "name": "anatomy", "category": "technical_explanation", "classification": "needs_manual_review", "recommended_roles": ["document_producer", "assistant_coordinator"]},
        ],
        "supported_runtimes": ["jarvis_native (guidance)", "claude_code", "codex"],
        "dependencies": ["Node.js 20.10+", "Chrome or Chromium (headless audit/screenshot scripts)", "Playwright/browser tooling"],
        "scripts_found": ["kit/*.mjs", "scripts/capture.mjs", "scripts/drive.mjs", "scripts/lines.mjs", "scripts/print.mjs", "examples/*/build.mjs"],
        "hooks_or_mcp": [],
        "external_commands": ["node build.mjs --audit", "node scripts/lines.mjs", "optional gallery publish when requested"],
        "install_instructions_summary": "Manual review required before installation; Chromium runtime dependencies missing from default offline OpenCode container.",
        "security_findings": [
            "MIT licensed with 0 hooks and 0 MCP configs, but bundles 98 scripts (print-sheet.test.mjs flagged dynamic_code_exec) and external gallery publishing guidance.",
            "Declared dependency on Node 20.10+ + Chrome/Chromium makes OpenCode container execution incompatible verbatim.",
        ],
        "jarvis_compatibility": "PARTIAL / OPENCODE INCOMPATIBLE — Guidance text loads in Jarvis native / Codex; full SVG/WebGL audit kit requires Chromium.",
        "recommendation": "NEEDS MANUAL REVIEW — Optional diagramming toolkit; quarantine scripts and block external gallery publishing under Local Mode.",
        "policy_verdict": "needs_manual_review",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
    {
        "source_id": "nutlope-hallmark",
        "repo": "https://github.com/nutlope/hallmark",
        "pinned_commit": "04977e15f3dfd6ee4b74c887dac3eb52665d6c92",
        "commit_date": "2026-11-11",
        "license": "mit",
        "tracked_files": 860,
        "skill_md_count": 1,
        "script_count": 50,
        "individual_skills": [
            {"skill_id": "hallmark", "subpath": "skills/hallmark", "name": "hallmark", "category": "ui_review", "classification": "approved_with_restrictions", "recommended_roles": ["coder_agent", "qa_agent"]},
        ],
        "supported_runtimes": ["jarvis_native (instruction_only)", "claude_code", "codex"],
        "dependencies": ["none in instruction_only; bundled scripts use Chromium & optional TOGETHER_API_KEY"],
        "scripts_found": [
            "skills/hallmark/scripts/install-hook.mjs (mutates settings.json!)",
            "skills/hallmark/scripts/lint-hook.mjs",
            "skills/hallmark/scripts/capture.mjs",
            "skills/hallmark/scripts/imagegen.mjs",
            "skills/hallmark/scripts/sloplint.mjs",
        ],
        "hooks_or_mcp": [".claude-plugin/marketplace.json", ".claude-plugin/plugin.json", "skills/hallmark/scripts/install-hook.mjs"],
        "external_commands": ["node install-hook.mjs (writes PostToolUse hook into .claude/settings.json)", "node imagegen.mjs (Together API)", "node capture.mjs"],
        "install_instructions_summary": "Approve static Floor CSS/a11y rules in SKILL.md ONLY under instruction_only mode; quarantine all scripts/hooks and disable imagegen.",
        "security_findings": [
            "MIT licensed (`skills/hallmark/LICENSE`), static Floor rules in SKILL.md are safe frontend craft constraints.",
            "Scanned 7 warnings: 6 .mjs files trigger dynamic_code_exec, `install-hook.mjs` mutates `~/.claude/settings.json`, `imagegen.mjs` calls external APIs (`TOGETHER_API_KEY`), and `capture.mjs` fails OpenCode container compat.",
        ],
        "jarvis_compatibility": "RESTRICTED — Compatible with jarvis_native / Codex in instruction_only mode; verbatim OpenCode container fails headless browser dependency.",
        "recommendation": "APPROVED WITH RESTRICTIONS — Install `skills/hallmark` rules strictly in instruction_only mode with all scripts/hooks/network calls quarantined.",
        "policy_verdict": "approved_with_restrictions",
        "lifecycle_defaults": {
            "audited": True,
            "approved": False,
            "installed": False,
            "enabled": False,
            "assigned": False,
            "successfully_executed": False,
        },
    },
]


def map_verification_to_classification(verification_status: str, *, runtime_compat: dict | None = None) -> str:
    """Map low-level scanner status into the strict P10.13 6-class audit classification."""
    st = (verification_status or "").strip().lower()
    if st == "blocked":
        return "blocked"
    if runtime_compat and all(not bool(v.get("compatible")) for v in runtime_compat.values()):
        return "incompatible"
    if st == "verified":
        return "approved"
    if st in ("approval_required", "restricted"):
        return "approved_with_restrictions"
    if st in SKILL_AUDIT_CLASSIFICATIONS:
        return st
    return "needs_manual_review"


def enrich_catalog_with_registry(installed_skills: list[dict], assignments: list[dict], metrics: dict[str, dict]) -> list[dict]:
    """Attach truthful live DB lifecycle flags (`audited`, `approved`, `installed`, `enabled`, `assigned`, `successfully_executed`)."""
    by_id = {s["skill_id"]: s for s in (installed_skills or [])}
    assigned_ids = {a["skill_id"] for a in (assignments or []) if a.get("enabled", True)}
    enriched: list[dict] = []
    for entry in RECOMMENDED_SKILL_AUDIT_CATALOG:
        item = dict(entry)
        matched_rows = []
        any_approved = False
        any_installed = False
        any_enabled = False
        any_assigned = False
        any_exec_ok = False
        for ind in item.get("individual_skills") or []:
            sid = ind["skill_id"]
            sk = by_id.get(sid)
            if sk is not None:
                m = metrics.get(sid) or {}
                inv = int(m.get("invocations") or 0)
                sr = float(m.get("success_rate") or 0.0)
                row_approved = bool(sk.get("approval_required", True)) and sk.get("verification_status") != "blocked"
                row_enabled = bool(sk.get("enabled"))
                row_assigned = sid in assigned_ids
                row_exec_ok = inv > 0 and sr > 0.0
                any_approved = any_approved or row_approved
                any_installed = True
                any_enabled = any_enabled or row_enabled
                any_assigned = any_assigned or row_assigned
                any_exec_ok = any_exec_ok or row_exec_ok
                matched_rows.append(
                    {
                        "skill_id": sid,
                        "installed": True,
                        "approved": row_approved,
                        "enabled": row_enabled,
                        "assigned": row_assigned,
                        "successfully_executed": row_exec_ok,
                        "invocations": inv,
                    }
                )
        item["matched_installed"] = matched_rows
        item["lifecycle_states"] = {
            "audited": True,
            "approved": any_approved,
            "installed": any_installed,
            "enabled": any_enabled,
            "assigned": any_assigned,
            "successfully_executed": any_exec_ok,
        }
        enriched.append(item)
    return enriched


def list_audit_reports() -> list[dict]:
    return [dict(item) for item in RECOMMENDED_SKILL_AUDIT_CATALOG]


def get_audit_report(source_id: str) -> dict:
    for item in RECOMMENDED_SKILL_AUDIT_CATALOG:
        if item["source_id"] == source_id:
            return dict(item)
    raise KeyError(source_id)
