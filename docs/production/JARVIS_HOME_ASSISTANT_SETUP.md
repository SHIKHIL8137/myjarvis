# Home Assistant (MCP preset)

**Status:** package, command and environment variables were **checked against the authoritative sources** (ha-mcp README on GitHub and the official Home Assistant "Model Context Protocol" integration page, 2026-11-13). Policy is tested against a local fake HA-like MCP server. **Not run against a live Home Assistant** (no HA credentials here).

### What the sources say (and what it means for JARVIS)
* `ha-mcp` (<https://github.com/homeassistant-ai/ha-mcp>): run with `uvx --python 3.14 ha-mcp@latest`; configuration via `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` (a long-lived access token). It sets MCP tool annotations including `readOnlyHint`, which is what JARVIS' read/write classification uses.
* The README calls the **stdio transport "not recommended"** (known connection problems; streamable HTTP is preferred). JARVIS' MCP client is stdio-only, so expect occasional connection trouble; the supervisor restarts it on demand (bounded).
* **`@latest` is unpinned**: `uvx` fetches and runs whatever is newest on every start, and the first start downloads Python 3.14 and packages. That is remote code execution by design — pin a reviewed version (`ha-mcp@<version>`) in Settings once you have chosen one. JARVIS only allows this server to start in Online Assisted mode.
* Home Assistant also has an **official** "Model Context Protocol" integration (HA is the MCP server) at `http://<host>:8123/api/mcp` over HTTP with OAuth 2.0; `/api/mcp/assist` exposes only the entities you exposed to voice assistants, and the integration has a **Read-only** option. JARVIS cannot connect to it directly (HTTP + OAuth are not supported by this client); a stdio bridge would be needed and is **not** provided or tested.

## What it is
A disabled-by-default MCP server preset (`jarvis/integrations/mcp_presets.py`). It adds no code path of its own: all policy is the generic MCP policy in `JARVIS_MCP_CONFIGURATION_AND_SECURITY.md`, applied to the tools the server *discovers* (the preset never lists tool names).

## Policy
| Action | Requirement |
|---|---|
| Read device state (tool marked read-only by the server, no write verb) | allowed, audited. Reading a lock's state is a read |
| Any device-changing call | owner approval via the existing Approvals (`mcp_write`) |
| Change touching `lock unlock cover garage door gate alarm siren arm disarm alarm_control_panel valve` (editable `critical_keywords`) | approval **and** spoken `confirm <code>` (voice only) |
| Unknown tool | treated as device-changing |
| Offline mode (default) | server refuses to start (`network: true`) until you enable Online Assisted mode |

The LLM can only *request*; it cannot approve, and a typed message or the assistant's own TTS echo cannot give the spoken confirmation.

## Setup
1. Home Assistant → profile → **Long-lived access tokens** → create one. Never paste it into JARVIS settings.
2. Set environment variables for the account that runs JARVIS (PowerShell, persistent):
   `setx HOMEASSISTANT_URL "http://homeassistant.local:8123"` and `setx HOMEASSISTANT_TOKEN "<token>"`, then restart JARVIS.
3. Install [uv](https://docs.astral.sh/uv/) (provides `uvx`) yourself. The preset runs `uvx --python 3.14 ha-mcp@latest`; replace `latest` with a version you reviewed (Settings → edit args).
4. Settings → *MCP servers* → **Add (disabled)** on the `homeassistant` preset.
5. Turn on **Online Assisted** mode (Settings → mode). Enable the server, press **Start**; discovered tools appear with their risk (`read`/`write`/`critical`).
6. Ask: "what is the state of the living room light" (runs directly). "Turn off the light" → an approval appears; approve it. "Unlock the front door" → approve, then say "confirm four eight two one" (the code shown in the approval).

## Manual verification (not done by me)
* A read returns state and appears in `logs\external_audit.jsonl` as `mcp_call` with `allowed=true`.
* A light change shows an approval and does nothing until approved.
* A lock/cover/alarm change shows the code, needs voice confirmation, and the audit shows `mcp_spoken_confirm`.
* Disable the server and confirm no process remains (`Get-Process uvx, python`).

## Security notes
* Use a dedicated HA user with only the entities you want JARVIS to reach; revoke the token to cut access instantly.
* Entity states are untrusted text (an attacker-named device could contain instructions); results are wrapped and redacted before the model sees them.
* Physical-safety actions are never executed solely on the LLM's request.
