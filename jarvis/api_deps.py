"""FastAPI dependency injection providers replacing stale router closures."""
from fastapi import Request


def get_assistant(request: Request):
    """Resolve the active Assistant / service container dynamically from app.state."""
    return request.app.state.a


def make_assistant_dep(fallback=None):
    """Return a FastAPI Depends callable resolving request.app.state.a with optional test fallback."""
    def _resolve(request: Request):
        return getattr(request.app.state, "a", None) or fallback
    return _resolve
