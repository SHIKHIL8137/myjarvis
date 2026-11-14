# MCP client — configuration and security

Implementation: `jarvis/mcp_client.py` (official `mcp` SDK 2.3.0, verified on Python 3.14.6). Presets: `jarvis/integrations/mcp_presets.py`.
Wired into: `Assistant` (tool exposure + execution), `api_v2` (`/api/v2/mcp/*`, shared approvals endpoint), Settings UI (*Settings → MCP servers*), health page.

## Configuration (`Settings.mcp`, stored in `data/settings.json`)

```json
{
  "mcp": {
    "servers": {
      "files": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/notes"],
        "env": { "SOME_TOKEN": "${SOME_TOKEN}" },
        "enabled": false,
        "startup_timeout_s": 15,
        "call_timeout_s": 30,
        "allowed_tools": [],
        "read_only_tools": [],
        "critical_keywords": [],
        "network": false,
        "max_restarts": 3
      }
    },
    "max_running": 8,
    "max_result_chars": 8000
  }
}
```

| Field | Meaning |
|---|---|
| name (key) | `[a-z0-9_-]{1,32}`; tools are exposed as `<name>.<tool>` |
| `command`, `args`, `env` | stdio child process. `env` values must be `${ENV_VAR}` references: literal values for key/token/secret/password-like names are **rejected** by the settings API; the UI only ever shows `${…}`/`••••` |
| `enabled` | **false by default**. A disabled server is never spawned and its start endpoint returns 409 |
| `startup_timeout_s`, `call_timeout_s` | hard limits; a timed-out call recycles the session |
| `allowed_tools` | empty = every discovered tool (still approval-gated); non-empty = exposure allow-list |
| `read_only_tools` | tools you vouch for as read-only (skip approval) |
| `critical_keywords` | words (matched against tool name and argument values of *state-changing* calls) that additionally require spoken confirmation |
| `network` | server talks to the network/LAN. While `offline_enforced` is on and Online Assisted mode is off the server **will not start** and calls are refused |
| `max_restarts` | bounded crash recovery; after that the server is `failed` until you restart it manually |

## Lifecycle

* Persistent stdio session per server, owned by one asyncio task (SDK requirement). Tools are discovered at start.
* Crash/timeout/cancel → session recycled; the **next** call restarts it on demand, at most `max_restarts` times. No background respawn loop; `max_running` caps concurrent processes.
* Graceful shutdown on app stop (`MCPManager.shutdown`), per-server `stop`/`restart` endpoints and buttons.

## Permission model

| Tool class | How decided | Behaviour |
|---|---|---|
| read | in `read_only_tools`, **or** the server marks `readOnlyHint` **and** the name has no write verb (delete/send/write/set/lock/…) | runs immediately, audited |
| write | write verb in name, or unknown/unannotated | creates an `ApprovalService` request (`mcp_write`, tier EXECUTE); runs only after the owner approves in the existing Approvals UI/endpoint; token is single-use and bound to the action type |
| critical | write **and** matches `critical_keywords` | additionally needs a **spoken** `confirm <4-digit code>` from the voice path (typed text, wrong code or JARVIS's own echo never count) |

Unknown tools are write by default. A server lying about `readOnlyHint` cannot downgrade a tool whose name implies a change.

## Data handling

* All results are untrusted: secrets/e-mail redacted (`pii_firewall`), wrapped by `wrap_untrusted` (`external_tool_output`), truncated to `max_result_chars`. Spoofed boundary/role tags inside results are neutralised.
* `memory_safe()` (`jarvis/security/untrusted.py`) is the only sanctioned way to put external text in memory.
* Audit: every start/stop/call/denial/approval request/spoken confirmation → `jarvis.audit.record_external` (`logs/external_audit.jsonl`). **Argument values are never logged**, only argument names.
* Env values and credentials are never returned by the API, written to settings, logged or sent to the model.

## Known limits

* Only stdio servers (the SDK also supports HTTP/SSE; not exposed). MCP **resources/prompts** are not surfaced to the brain.
* Tool calls are only offered on the local-Ollama tool loop (the cloud gateway path in `_iter_tool_turn` does not pass tools — pre-existing behaviour).
* A stdio child is an arbitrary program running as you. Only add servers you trust; the offline guard (`audit` hook) protects the JARVIS process, **not** the child's own network use — that is what the `network` flag gates at start-up.
* Server-side `sampling`/`elicitation` requests are not serviced.
