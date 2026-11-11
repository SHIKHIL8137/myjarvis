"""Conversation history: persisted to data/history.jsonl, windowed for the prompt (context is only 4096 tokens)."""
import json
import threading
from pathlib import Path

MAX_CHARS = 2000  # ~500 tokens. Measured: this GPU reads ~250 prompt tokens/s, so 5000 chars of history cost ~4 s per turn


class History:
    def __init__(self, path: Path, turns: int = 6):
        self.path, self.turns = Path(path), turns
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._start = 0  # index of the first message in the current prompt window
        if self.path.exists():
            for ln in self.path.read_text(encoding="utf-8").splitlines()[-200:]:
                try:
                    self.items.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass  # a torn last line after a crash must not block startup

    def add(self, role: str, content: str) -> None:
        item = {"role": role, "content": content}
        with self._lock:
            self.items.append(item)
            if len(self.items) > 400:  # the file keeps everything; memory keeps the recent 200
                drop = len(self.items) - 200
                del self.items[:drop]
                self._start = max(0, self._start - drop)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def window(self) -> list[dict]:
        """Recent messages, at most `turns` exchanges and MAX_CHARS characters.
        The window GROWS turn by turn and, when full, drops its oldest half in one step. A window that slid every turn
        would change the start of the prompt each time and force Ollama to re-read all of it (no KV-cache reuse)."""
        if not self.turns:
            return []
        with self._lock:
            def over(start, count, chars):
                w = self.items[start:]
                return len(w) > count or sum(len(m["content"]) for m in w) > chars
            if over(self._start, 2 * self.turns, MAX_CHARS):
                while self._start < len(self.items) and (over(self._start, self.turns, MAX_CHARS // 2)
                                                         or self.items[self._start]["role"] != "user"):
                    self._start += 1
            return list(self.items[self._start:])

    def clear(self) -> None:
        with self._lock:
            self.items.clear()
            self._start = 0
            self.path.write_text("", encoding="utf-8") if self.path.exists() else None
