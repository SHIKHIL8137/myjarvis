"""Hardware-aware concurrency & queue governance (P10.9: protects GTX 1650 4GB VRAM / 16GB RAM)."""
import asyncio
from contextlib import asynccontextmanager
import threading


class ResourceQueueFullError(RuntimeError):
    """Raised when concurrent queued inference requests exceed safe hardware capacity."""


class ResourceGovernor:
    """Bounds simultaneous heavy local model inference and cloud concurrency cleanly."""

    def __init__(self, *, max_local: int = 1, max_cloud: int = 3, max_queue: int = 16):
        self.max_local = max(1, int(max_local))
        self.max_cloud = max(1, int(max_cloud))
        self.max_queue = max(1, int(max_queue))
        self._lock = threading.Lock()
        self._queued = 0
        self._active_local = 0
        self._active_cloud = 0
        self._local_sem = asyncio.Semaphore(self.max_local)
        self._cloud_sem = asyncio.Semaphore(self.max_cloud)

    @asynccontextmanager
    async def acquire(self, *, local: bool = True, timeout_s: float = 30.0):
        with self._lock:
            if self._queued >= self.max_queue:
                raise ResourceQueueFullError(
                    f"Inference queue full ({self._queued}/{self.max_queue}); protecting host memory stability."
                )
            self._queued += 1

        sem = self._local_sem if local else self._cloud_sem
        try:
            await asyncio.wait_for(sem.acquire(), timeout=max(0.5, float(timeout_s)))
            with self._lock:
                self._queued = max(0, self._queued - 1)
                if local:
                    self._active_local += 1
                else:
                    self._active_cloud += 1
            try:
                yield
            finally:
                with self._lock:
                    if local:
                        self._active_local = max(0, self._active_local - 1)
                    else:
                        self._active_cloud = max(0, self._active_cloud - 1)
                sem.release()
        except Exception:
            with self._lock:
                self._queued = max(0, self._queued - 1)
            raise

    def stats(self) -> dict:
        with self._lock:
            return {
                "max_local_concurrency": self.max_local,
                "max_cloud_concurrency": self.max_cloud,
                "max_queue_size": self.max_queue,
                "active_local": self._active_local,
                "active_cloud": self._active_cloud,
                "queued": self._queued,
            }

