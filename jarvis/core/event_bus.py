"""Thread-safe asyncio pub/sub event bus bridging voice, agents, training, and SSE clients."""
import asyncio
import logging

log = logging.getLogger("jarvis.events")


class EventBus:
    """Fan-out of UI/telemetry events to SSE subscribers; publish() is safe from any thread."""

    def __init__(self, max_queue: int = 200):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.max_queue = max_queue
        self.subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def publish(self, ev: dict) -> None:
        def _put():
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    pass  # a stuck client must not stall the app

        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(_put)
            except RuntimeError:
                pass

