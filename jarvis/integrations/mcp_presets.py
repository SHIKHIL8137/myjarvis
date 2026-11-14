"""MCP server presets (configuration only - no code path of their own).

A preset is a disabled-by-default `MCPServerSpec` template. Installing one writes it through the normal settings API; the owner then
supplies credentials as environment variables (`${NAME}` references, never literal secrets) and enables it. All policy (approvals, spoken
confirmation, offline gating, audit, untrusted output) is enforced by `jarvis.mcp_client.MCPManager` using the *discovered* tool
metadata - presets never list tool names.
"""
from __future__ import annotations

from ..config import MCPServerSpec

# Home Assistant community MCP server "ha-mcp" (https://github.com/homeassistant-ai/ha-mcp). Command and environment variables below were
# checked against its README (2026-11): `uvx --python 3.14 ha-mcp@latest` with HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN. Its README calls the
# stdio transport "not recommended" (streamable HTTP is preferred); JARVIS only speaks stdio. `@latest` is unpinned: pin a version you have
# reviewed (see docs/production/JARVIS_HOME_ASSISTANT_SETUP.md). The first start makes uv download Python/packages (network).
HOMEASSISTANT = MCPServerSpec(
    command="uvx",
    args=["--python", "3.14", "ha-mcp@latest"],
    env={"HOMEASSISTANT_URL": "${HOMEASSISTANT_URL}", "HOMEASSISTANT_TOKEN": "${HOMEASSISTANT_TOKEN}"},
    enabled=False,
    network=True,  # talks to your Home Assistant over the LAN: refused while offline mode is enforced
    startup_timeout_s=60,
    call_timeout_s=20,
    # Device *domains/actions* that need spoken confirmation in addition to approval. Matched against tool names and argument
    # values of state-changing calls; reading these devices' state is unaffected. Edit freely in Settings.
    critical_keywords=["lock", "unlock", "cover", "garage", "door", "gate", "alarm", "siren", "arm", "disarm", "alarm_control_panel", "valve"],
    description="Home Assistant smart-home control. Reads are allowed when the server marks them read-only; every change needs approval.",
)

PRESETS: dict[str, MCPServerSpec] = {"homeassistant": HOMEASSISTANT}


def preset_config(name: str) -> dict:
    """Settings-patch payload that installs the preset (still disabled)."""
    spec = PRESETS[name]
    return {"mcp": {"servers": {name: spec.model_dump()}}}
