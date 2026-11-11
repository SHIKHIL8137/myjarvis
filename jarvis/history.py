"""Conversation history: persisted to data/history.jsonl, windowed for the prompt (context is only 4096 tokens),
plus full-file timestamped keyword search across historical conversations."""
import json
import threading
import time
from pathlib import Path

from .retrieval import tokens

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
                    raw = json.loads(ln)
                    if isinstance(raw, dict) and "role" in raw and "content" in raw:
                        self.items.append({"role": raw["role"], "content": raw["content"], **({"t": raw["t"]} if "t" in raw else {})})
                except json.JSONDecodeError:
                    pass  # a torn last line after a crash must not block startup

    def add(self, role: str, content: str) -> None:
        item = {"role": role, "content": content, "t": round(time.time(), 3)}
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
        """Recent messages, at most `turns` exchanges and MAX_CHARS characters (returns strict {role, content} dicts)."""
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
            return [{"role": m["role"], "content": m["content"]} for m in self.items[self._start:]]

    def search_disk(self, query: str, days: int | None = None, limit: int = 8) -> list[dict]:
        """Search all persisted conversation turns on disk (including turns evicted from the live 200-item RAM slice)."""
        q_toks = set(tokens(query))
        if not q_toks or not self.path.exists():
            return []
        cutoff = (time.time() - days * 86400.0) if days is not None else None
        matches: list[tuple[float, dict]] = []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        for ln in lines:
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            ts = rec.get("t")
            if cutoff is not None and ts is not None and float(ts) < cutoff:
                continue
            body = str(rec.get("content", ""))
            doc_toks = set(tokens(body))
            overlap = len(q_toks & doc_toks)
            if not overlap:
                continue
            score = overlap / max(1.0, len(q_toks) ** 0.5)
            matches.append((score, {"role": rec.get("role", "user"), "content": body, "t": ts, "score": round(score, 3)}))
        matches.sort(key=lambda x: (-x[0], -(x[1].get("t") or 0)))
        return [m[1] for m in matches[: max(1, limit)]]

    def clear(self) -> None:
        with self._lock:
            self.items.clear()
            self._start = 0
            self.path.write_text("", encoding="utf-8") if self.path.exists() else None
