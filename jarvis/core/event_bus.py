"""Thread-safe asyncio pub/sub event bus bridging voice, agents, training, and SSE clients."""
from collections import deque
from datetime import datetime, timezone
import asyncio
import logging
import threading

log = logging.getLogger("jarvis.events")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventBus:
    """Fan-out of UI/telemetry events to SSE subscribers with monotonic seq & reconnect replay buffer."""

    def __init__(self, max_queue: int = 200, history_limit: int = 600):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.max_queue = max_queue
        self.subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._seq = 0
        self._history: deque[dict] = deque(maxlen=history_limit)

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def events_since(self, since_seq: int = 0) -> tuple[list[dict], bool]:
        """Return buffered events strictly newer than `since_seq` and whether a history gap occurred."""
        with self._lock:
            if not self._history:
                return [], bool(since_seq and since_seq < self._seq)
            oldest = int(self._history[0].get("seq", 0))
            gap = bool(since_seq > 0 and since_seq < oldest - 1)
            missed = [dict(ev) for ev in self._history if int(ev.get("seq", 0)) > since_seq]
            return missed, gap

    def publish(self, ev: dict) -> dict:
        with self._lock:
            self._seq += 1
            seq = self._seq
            stamped = dict(ev)
            stamped.setdefault("schema_version", 1)
            stamped.setdefault("seq", seq)
            stamped.setdefault("event_id", f"evt-{seq}")
            stamped.setdefault("occurred_at", utc_iso())
            stamped.setdefault("event_type", stamped.get("event") or stamped.get("type") or "system.event")
            self._history.append(stamped)

        def _put():
            for q in list(self.subs):
                try:
                    q.put_nowait(stamped)
                except asyncio.QueueFull:
                    pass  # a stuck client must not stall the app

        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(_put)
            except RuntimeError:
                pass
        return stamped


