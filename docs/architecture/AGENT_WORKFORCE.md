# Markdown Agent Profiles & Workforce Coordination (P3)

## Overview

The P3 Workforce Coordinator loads agent specifications defined as Markdown files with YAML frontmatter. It enforces deterministic selection, tool sandboxing, and bounded delegation without privilege escalation.

## Profile Loading & Precedence

Agent profiles are discovered across three hierarchical scopes:
1. **Project Scope** (`.jarvis/agents/*.md`, `.agents/*.md`): Highest precedence. Allows project-specific customization of prompts, tools, and delegation limits.
2. **User Scope** (`data/agents/*.md` or `settings.agents_dir`): User-level customizations and overrides.
3. **Built-in Scope** (`jarvis/agents/builtin_agents/*.md`): Standard shipping profiles:
   - `research_specialist.md`: Read-only deep research and fact verification (`READ` tier).
   - `dev_coordinator.md`: Code architecture and execution coordinator (`EXECUTE` tier).
   - `assistant_general.md`: Primary conversational assistant (`EXECUTE` tier).

Malformed Markdown files are safely skipped with diagnostic warnings recorded in `AgentWorkforceCoordinator.get_diagnostics()`, preventing server crashes.

## Frontmatter Specification

```yaml
---
name: research_specialist
description: Gathers evidence and checks claims against retrieved sources
model_role: fast
permission_tier: READ
tools_allowlist: [search_web, deep_research, read_file, list_directory]
tools_denylist: [execute_shell, write_file, delete_file]
max_steps: 16
max_tokens: 32000
can_delegate: false
max_delegation_depth: 0
---
System instructions for the agent go here in standard Markdown.
```

## Security & Sandboxing Rules

### 1. Deterministic Tool Filtering

The coordinator computes effective agent tools as:
$$\text{effective\_tools} = (\text{agent\_tools} \cap \text{tools\_allowlist}) \setminus \text{tools\_denylist}$$
Dangerous tools (such as shell execution or file modification) cannot be invoked by lower-tier agents even if prompted.

### 2. Privilege Escalation Prevention

Agents operate under an explicit privilege hierarchy:
$$\text{READ} < \text{PROMPT} < \text{EXECUTE} < \text{ADMIN}$$
- An agent may only delegate to an agent whose `permission_tier` is **less than or equal to** its own.
- Any attempt by a `READ`-tier agent to invoke an `EXECUTE` or `ADMIN`-tier agent is rejected immediately with a privilege escalation denial.

### 3. Bounded Delegation Depth

- Each profile specifies `max_delegation_depth` (default 1, max 3).
- When `current_depth >= max_delegation_depth`, delegation requests are rejected, preventing infinite delegation cycles.

