# JARVIS AGENT RUNTIME REGISTRY & DYNAMIC DISCOVERY SPECIFICATION
**Version**: 2.0 (Production Standard)  
**Status**: Authoritative Architectural Standard  
**Component**: `jarvis.core.runtime_registry.RuntimeRegistryService`  

---

## 1. Domain Separation Model

To prevent architectural conflation, JARVIS strictly distinguishes between 7 domain concepts:

```
+---------------------------------------------------------------------------------------------------+
| 1. PROVIDER ORGANIZATIONS (e.g., OpenRouter, OpenAI, Anthropic, Google, Ollama)                   |
|    - Supply model inference APIs and rate limits                                                  |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| 2. MODELS (e.g., qwen3:4b-instruct, gpt-4o-mini, claude-3-5-sonnet)                               |
|    - Belong to provider catalogs; have context limits, modalities, and pricing tiers              |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| 3. AGENT APPLICATIONS (e.g., OpenCode, Claude Code, Antigravity)                                  |
|    - External execution products and CLI harnesses (NOT models; NOT providers)                    |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| 4. RUNTIME INSTANCES (e.g., C:\Users\shikh\AppData\Roaming\npm\opencode.cmd)                     |
|    - Physical executables discovered on host with verified version and auth state                 |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| 5. AGENT INSTANCES / SESSIONS (e.g., sess_1794508210_a1b2)                                       |
|    - Live, active execution sessions bound to a task worktree                                     |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| 6. AGENT PROFILES (e.g., architect, fullstack_dev, qa_engineer, security_reviewer)                |
|    - Reusable instruction templates, department roles, and permission scopes                      |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| 7. TASK ASSIGNMENTS                                                                               |
|    - Durable bindings between task steps and eligible runtimes, models, or tools                  |
+---------------------------------------------------------------------------------------------------+
```

---

## 2. Evidence Tiers for Dynamic Discovery

A runtime instance is never assumed to be ready simply because a binary name is typed. JARVIS uses 11 evidence tiers:

| Evidence Level | Criteria |
|---|---|
| `not_discovered` | Binary executable not found on host `PATH` or standard search directories. |
| `installed` | Executable file found on disk, but `--version` probe has not yet run. |
| `version_verified` | Executable successfully responded to `--version` probe with a valid version string. |
| `auth_verified` | Authentication credentials or active login session validated non-destructively. |
| `protocol_initialized` | Handshake completed with agent process (e.g. MCP initialize or CLI JSON pipe). |
| `capability_advertised` | Runtime manifest or documentation advertises support for the requested capability. |
| `capability_tested` | Safe, non-destructive test probe verified the capability in an isolated sandbox. |
| `temporarily_unavailable` | Host resource exhaustion, rate limit, or lock contention temporarily preventing execution. |
| `auth_expired` | Session token or API key returned 401 Unauthorized or expired token message. |
| `unsupported` | Host OS, architecture, or container prerequisites missing for this runtime. |
| `unknown` | Initializing or unprobed state. |

---

## 3. Real Discovery Probe Output on Current Host

```json
{
  "counts": {
    "installed_runtimes": 2,
    "authenticated_runtimes": 2,
    "healthy_runtimes": 2,
    "active_agent_sessions": 0,
    "available_agent_profiles": 5,
    "configured_providers": 2
  },
  "runtimes": [
    {
      "app_id": "opencode",
      "display_name": "OpenCode CLI",
      "executable_path": "C:\\Users\\shikh\\AppData\\Roaming\\npm\\opencode.cmd",
      "evidence_level": "auth_verified",
      "authenticated": true,
      "healthy": true
    },
    {
      "app_id": "claude_code",
      "display_name": "Claude Code",
      "executable_path": "C:\\Users\\shikh\\.local\\bin\\claude.exe",
      "evidence_level": "auth_verified",
      "authenticated": true,
      "healthy": true
    },
    {
      "app_id": "antigravity",
      "display_name": "Antigravity",
      "executable_path": null,
      "evidence_level": "not_discovered",
      "authenticated": false,
      "healthy": false
    }
  ]
}
```

---

## 4. API Endpoints
- `GET /api/v2/runtimes`: Returns full discovery snapshot, evidence breakdown, configured providers, and agent profiles.
- `POST /api/v2/runtimes/discover`: Forces immediate re-scan of host PATH and credential directories.

