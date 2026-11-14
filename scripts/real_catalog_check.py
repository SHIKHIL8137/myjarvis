"""READ-ONLY real provider catalog check: GET https://openrouter.ai/api/v1/models (public, no key, no cost). No completion request is ever made.
OpenAI is only checked if a key is already configured (never printed). Prints counts only."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import audit  # noqa: E402
from jarvis.config import has_provider_api_key, load_settings  # noqa: E402
from jarvis.model_catalog import ModelCatalog  # noqa: E402


async def main():
    s = load_settings()
    s.internet.enabled = True
    s.providers.allow_cloud = True
    audit.install(enforce=True, online_enabled=True)
    brain = SimpleNamespace(current="local", spec=SimpleNamespace(model="x"), provider="ollama", installed=None)

    async def inst():
        return []

    brain.installed = inst
    cat = ModelCatalog(SimpleNamespace(settings=s, brain=brain))
    for prov in ("openrouter", "openai"):
        if prov == "openai" and not has_provider_api_key("openai"):
            print("openai: NOT TESTED (no key configured)")
            continue
        st = await cat.refresh(prov, force=True)
        free = sum(1 for m in st["models"] if m["free"])
        print(f"{prov}: status={st['status']} models={len(st['models'])} free={free} error={st.get('error')!r}")
        if prov == "openrouter" and st["models"]:
            print("  sample:", [m["model"] for m in st["models"][:3]])
            rec = cat.recommend("coding", st["models"])["recommendations"]
            print("  coding recs:", [(r["model"], r["free"]) for r in rec])


asyncio.run(main())
