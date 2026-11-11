"""Individual security, license, & compatibility audits for the 7 recommended external skill repositories.

Every commit SHA, license, file count, script path, and runtime constraint below was observed directly
by read-only repository inspection during P10.12 (never executed automatically; never installed blindly).
"""

RECOMMENDED_SKILL_AUDIT_CATALOG: list[dict] = [
    {
        "source_id": "cloudflare-security-audit",
        "repo": "https://github.com/cloudflare/security-audit-skill",
        "pinned_commit": "c1c8a8c1471069fb0e188eeaff69b8e8db6564a8",
        "license": "mit",
        "tracked_files": 22,
        "individual_skills": [
            {
                "skill_id": "cloudflare-security-audit",
                "subpath": "skills/security-audit",
                "name": "security-audit",
                "category": "security_audit",
                "recommended_roles": ["qa_agent", "coder_agent", "architect_agent"],
            }
        ],
        "supported_runtimes": ["jarvis_native", "opencode", "claude_code", "codex"],
        "dependencies": ["node (optional for validate-*.cjs)"],
        "scripts_found": [
            "skills/security-audit/validate-coverage-ledger.cjs",
            "skills/security-audit/validate-findings.cjs",
        ],
        "hooks_or_mcp": [],
        "external_commands": ["node validate-coverage-ledger.cjs", "node validate-findings.cjs"],
        "install_instructions_summary": "Copy skills/security-audit/ folder into project skills directory; no npm install or network installer.",
        "security_findings": [
            "Clean MIT skill with zero remote curl|sh installers, zero global hooks, and explicit Guidance vs Full-Audit consent check.",
            "Bundled .cjs validators are zero-dependency Node scripts; run only inside sandbox if enabled.",
        ],
        "jarvis_compatibility": "HIGH — Agent-neutral Markdown workflow integrates cleanly into Jarvis Dev Workforce security reviews & QA guidance without overriding worktree merge gates.",
        "recommendation": "RECOMMENDED (Priority 1) — Install `skills/security-audit` in instruction_only mode (optional Node validators inside Docker sandbox) upon owner approval.",
        "policy_verdict": "approve_recommended",
    },
    {
        "source_id": "mattpocock-skills",
        "repo": "https://github.com/mattpocock/skills",
        "pinned_commit": "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d",
        "license": "mit",
        "tracked_files": 184,
        "individual_skills": [
            {"skill_id": "mattpocock-tdd", "subpath": "skills/engineering/tdd", "name": "tdd", "category": "testing_api", "recommended_roles": ["coder_agent", "qa_agent"]},
            {"skill_id": "mattpocock-code-review", "subpath": "skills/engineering/code-review", "name": "code-review", "category": "code_review", "recommended_roles": ["qa_agent", "coder_agent"]},
            {"skill_id": "mattpocock-diagnosing-bugs", "subpath": "skills/engineering/diagnosing-bugs", "name": "diagnosing-bugs", "category": "debugging", "recommended_roles": ["coder_agent", "qa_agent"]},
            {"skill_id": "mattpocock-domain-modeling", "subpath": "skills/engineering/domain-modeling", "name": "domain-modeling", "category": "architecture", "recommended_roles": ["architect_agent"]},
        ],
        "supported_runtimes": ["jarvis_native", "opencode", "claude_code", "codex"],
        "dependencies": ["none (selected 4 engineering skills are pure Markdown + .md references)"],
        "scripts_found": [
            "scripts/link-skills.sh",
            "skills/misc/git-guardrails-claude-code/scripts/block-dangerous-git.sh (excluded)",
            "skills/engineering/diagnosing-bugs/scripts/hitl-loop.template.sh (reference template only)",
        ],
        "hooks_or_mcp": [".claude-plugin/marketplace.json", ".claude-plugin/plugin.json"],
        "external_commands": ["link-skills.sh symlink installer (DO NOT RUN blindly)"],
        "security_findings": [
            "The 4 prioritized engineering skills (tdd, code-review, diagnosing-bugs, domain-modeling) are clean instruction Markdown files.",
            "Repo also contains Claude-specific git hook skill (`git-guardrails-claude-code`) and symlink installer (`scripts/link-skills.sh`), which MUST be skipped because Jarvis enforces worktree Git safety natively.",
        ],
        "jarvis_compatibility": "HIGH (Selective) — Extract the 4 prioritized engineering skills individually; do NOT install repo-wide Claude plugin hooks.",
        "recommendation": "RECOMMENDED (Priority 1, Selective) — Install `tdd`, `code-review`, `diagnosing-bugs`, `domain-modeling` via owner approval.",
        "policy_verdict": "approve_selective",
    },
    {
        "source_id": "dietrichgebert-ponytail",
        "repo": "https://github.com/DietrichGebert/ponytail",
        "pinned_commit": "96608e078b2d5ea607c5c3b8b5a54d1c1fba6846",
        "license": "mit",
        "tracked_files": 216,
        "individual_skills": [
            {"skill_id": "ponytail", "subpath": "skills/ponytail", "name": "ponytail", "category": "refactoring", "recommended_roles": ["coder_agent"]},
            {"skill_id": "ponytail-review", "subpath": "skills/ponytail-review", "name": "ponytail-review", "category": "refactoring", "recommended_roles": ["qa_agent", "coder_agent"]},
        ],
        "supported_runtimes": ["jarvis_native", "opencode", "claude_code", "codex"],
        "dependencies": ["none for SKILL.md guidance; hooks require Node if harness hooks enabled"],
        "scripts_found": [
            "hooks/ponytail-activate.js",
            "hooks/ponytail-runtime.js",
            "hooks/ponytail-statusline.sh",
            "hooks/ponytail-statusline.ps1",
        ],
        "hooks_or_mcp": ["plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "hooks/claude-codex-hooks.json"],
        "external_commands": ["Plugin/hook installers across Claude/Cursor/Codex (skip global hook install)"],
        "security_findings": [
            "Core `skills/ponytail/SKILL.md` minimal-diff discipline is safe guidance text.",
            "60 supporting JS/PS1/SH hook & benchmark files (`hooks/`) mutate session prompt injection if installed globally; Jarvis Router renders these unnecessary and quarantines hooks.",
        ],
        "jarvis_compatibility": "HIGH (Guidance Extracted) — Compatible as minimal-scope refactoring instruction skill once harness hooks are excluded.",
        "recommendation": "RECOMMENDED (Priority 1, Instruction-Only) — Extract `skills/ponytail/SKILL.md` cleanly with hooks excluded.",
        "policy_verdict": "approve_selective",
    },
    {
        "source_id": "pbakaus-impeccable",
        "repo": "https://github.com/pbakaus/impeccable",
        "pinned_commit": "c1e0e5492fbb524f5df84d635ecfae7dc438ec45",
        "license": "apache-2.0",
        "tracked_files": 4527,
        "individual_skills": [
            {"skill_id": "impeccable", "subpath": "plugin/skills/impeccable", "name": "impeccable", "category": "react_frontend", "recommended_roles": ["coder_agent"]},
        ],
        "supported_runtimes": ["jarvis_native", "claude_code"],
        "dependencies": ["Self-contained launcher binary referenced via ${CLAUDE_SKILL_DIR}/scripts/impeccable"],
        "scripts_found": ["browser-bundle/*.js", "cli/bin/cli.js", "scripts/build*.js"],
        "hooks_or_mcp": [".claude-plugin/plugin.json", ".codex/hooks.json", "plugin/hooks/hooks.json"],
        "external_commands": ["`${CLAUDE_SKILL_DIR}/scripts/impeccable context` (downloads/runs binary launcher on first run!)"],
        "security_findings": [
            "CRITICAL AUDIT FINDING: `plugin/skills/impeccable/SKILL.md` instructs agents to execute `${CLAUDE_SKILL_DIR}/scripts/impeccable context`, documenting that 'The launcher runs a self-contained binary that ships next to it or is downloaded once on first run'.",
            "Jarvis MUST NOT allow unverified first-run binary downloads or global CLI installation; extract static `reference/craft-floor.md` design guidance ONLY or quarantine launcher strictly!",
        ],
        "jarvis_compatibility": "PARTIAL / INCOMPATIBLE NATIVE OPENCODE — Incompatible with OpenCode/Codex verbatim due to `${CLAUDE_SKILL_DIR}` binary launcher & Claude hooks; usable in Jarvis ONLY as sanitized design-floor guidance.",
        "recommendation": "SELECTIVE GUIDANCE ONLY — Strip binary launcher setup & hooks; load static UI design tokens/principles in instruction_only mode if owner approves.",
        "policy_verdict": "approve_guidance_only_quarantined",
    },
    {
        "source_id": "affaan-m-everything-claude-code",
        "repo": "https://github.com/affaan-m/everything-claude-code",
        "pinned_commit": "d2ef2680594f9a17526a6e1a2e18b3d242c5d17b",
        "license": "mit",
        "tracked_files": 4701,
        "individual_skills": [
            {"skill_id": "ecc-agent-security-hardening", "subpath": "skills/agent-security-hardening", "name": "agent-security-hardening", "category": "security_audit", "recommended_roles": ["architect_agent"]},
        ],
        "supported_runtimes": ["claude_code (primary framework)"],
        "dependencies": ["Node.js", "@iarna/toml", "ajv", "js-yaml", "sql.js", "zod", "external MCP servers"],
        "scripts_found": ["install.sh", "install.ps1", "scripts/ecc.js", "scripts/memory-mcp.mjs", "ecc_dashboard.py", "1298 total script/config files"],
        "hooks_or_mcp": [".mcp.json", "mcp-configs/mcp-servers.json", "hooks/hooks.json", "hooks/codex-hooks.json", ".opencode/index.ts"],
        "external_commands": ["install.ps1 / install.sh", "npx ecc-universal", "ecc-memory-mcp"],
        "security_findings": [
            "FRAMEWORK CONFLICT & RISK: Ships 4,701 files including global `install.sh`/`install.ps1`, custom memory MCP (`ecc-memory-mcp`), control-plane scripts, payment skills (`agent-payment-x402`), OSINT scripts, and harness hooks.",
            "Installing globally would duplicate & conflict with Jarvis's 5-way memory, approval gates, dashboard, and LangGraph orchestration.",
        ],
        "jarvis_compatibility": "INCOMPATIBLE AS FRAMEWORK — Must NEVER be installed wholesale into Jarvis or OpenCode worktrees; audit only single isolated Markdown reference skills if explicitly needed.",
        "recommendation": "DO NOT INSTALL WHOLESALE — Keep disabled/uninstalled; evaluate single static Markdown references selectively only if a capability is not already covered by Cloudflare/Matt Pocock skills.",
        "policy_verdict": "do_not_install_framework",
    },
    {
        "source_id": "wheresryan22-anatomy",
        "repo": "https://github.com/wheresryan22/anatomy",
        "pinned_commit": "1482e8469b0a9e0d8cc18ea2740a01423d29ae80",
        "license": "mit",
        "tracked_files": 173,
        "individual_skills": [
            {"skill_id": "anatomy", "subpath": ".", "name": "anatomy", "category": "technical_explanation", "recommended_roles": ["document_producer", "assistant_coordinator"]},
        ],
        "supported_runtimes": ["jarvis_native", "claude_code", "codex"],
        "dependencies": ["Node.js 20.10+", "Chrome/Chromium (for visual figure audit/PDF export)"],
        "scripts_found": ["kit/*.mjs", "examples/*/build.mjs", "scripts/*.mjs"],
        "hooks_or_mcp": [],
        "external_commands": ["node build.mjs --audit", "Chromium PDF print"],
        "security_findings": [
            "MIT license, zero hooks/MCP configs, explicitly documents `Node.js 20.10+` and `Chrome or Chromium` requirements.",
            "Includes optional gallery publish instruction ('publishes it to the Anatomy community gallery when asked') — blocked by Jarvis Local Mode unless explicitly permitted.",
        ],
        "jarvis_compatibility": "PARTIAL (Optional Docs) — Works in Jarvis documentation guidance mode cleanly; full interactive HTML/SVG kit checks require Node 20+ & headless Chromium (absent from default offline OpenCode image).",
        "recommendation": "OPTIONAL DOCUMENTATION CAPABILITY — Approve instruction-only mode for technical explanations; enable Node `kit/` build only in workspace when owner approves.",
        "policy_verdict": "optional_documentation",
    },
    {
        "source_id": "nutlope-hallmark",
        "repo": "https://github.com/nutlope/hallmark",
        "pinned_commit": "04977e15f3dfd6ee4b74c887dac3eb52665d6c92",
        "license": "mit",
        "tracked_files": 860,
        "individual_skills": [
            {"skill_id": "hallmark", "subpath": "skills/hallmark", "name": "hallmark", "category": "ui_review", "recommended_roles": ["coder_agent", "qa_agent"]},
        ],
        "supported_runtimes": ["jarvis_native", "claude_code"],
        "dependencies": ["Optional Playwright/Chromium (`capture.mjs`), optional image API (`imagegen.mjs`)"],
        "scripts_found": [
            "skills/hallmark/scripts/capture.mjs",
            "skills/hallmark/scripts/imagegen.mjs",
            "skills/hallmark/scripts/install.mjs",
            "skills/hallmark/scripts/install-hook.mjs",
            "skills/hallmark/scripts/lint-hook.mjs",
        ],
        "hooks_or_mcp": [".claude-plugin/marketplace.json", ".claude-plugin/plugin.json"],
        "external_commands": ["node capture.mjs", "imagegen.mjs (external API)", "install-hook.mjs"],
        "security_findings": [
            "MIT license. Root design rules ('The Floor': CSS tokens, `:focus-visible`, overflow rules) are safe static guidance.",
            "Bundled `install-hook.mjs` / `imagegen.mjs` / `capture.mjs` perform Playwright screenshots & external image generation calls; quarantine scripts by default.",
        ],
        "jarvis_compatibility": "HIGH (Design Floor Guidance) / PARTIAL (Scripts) — Extract `skills/hallmark/SKILL.md` rules without auto-running `install-hook.mjs` or `imagegen.mjs`.",
        "recommendation": "RECOMMENDED (Priority 1, Floor Rules Only) — Install `skills/hallmark` rules in instruction_only mode (scripts quarantined).",
        "policy_verdict": "approve_selective",
    },
]


def list_audit_reports() -> list[dict]:
    return list(RECOMMENDED_SKILL_AUDIT_CATALOG)


def get_audit_report(source_id: str) -> dict:
    for item in RECOMMENDED_SKILL_AUDIT_CATALOG:
        if item["source_id"] == source_id:
            return dict(item)
    raise KeyError(source_id)

