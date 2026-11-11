"""Fast deterministic voice/text commands that shouldn't wait on the LLM (delegates to intent.py)."""
from .intent import WEB_EXPLICIT, route_intent


async def route(a, text: str) -> dict | None:
    """Delegate deterministic routing cleanly to the centralized IntentRouter."""
    return await route_intent(a, text)
