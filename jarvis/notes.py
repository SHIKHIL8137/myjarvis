"""Markdown note store + knowledge graph. Folder = category. Live: re-scans when files change.
Deleting moves to notes/.trash (never hard-deletes)."""
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import uuid

WIKI = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
EXCERPT = 700
_NOTE_NS = uuid.UUID("a3e1f790-41d5-4d91-9b82-6c7a4d55e102")


@dataclass
class Note:
    id: int
    title: str
    category: str
    path: str  # relative to notes root, forward slashes
    body: str
    excerpt: str
    links: set = field(default_factory=set)  # lowercase titles referenced via [[wikilinks]]
    mtime_ns: int = 0
    uid: str = ""

    def __post_init__(self):
        if not self.uid:
            self.uid = str(uuid.uuid5(_NOTE_NS, self.path))


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:maxlen].strip("-") or "note"


def title_from(text: str, words: int = 7) -> str:
    return " ".join(text.split()[:words]).strip(" .,:;!?").capitalize() or "Untitled"


class NoteStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sig: tuple[tuple[str, int], ...] | None = None
        self._checked = 0.0
        self.notes: list[Note] = []
        self.version = 0  # bumps on every rebuild; cheap change detector for clients
        self.refresh(force=True)

    def _files(self):
        return sorted(p for p in self.root.rglob("*.md") if not any(part.startswith(".") for part in p.relative_to(self.root).parts))

    def refresh(self, force: bool = False) -> bool:
        with self._lock:
            if not force and time.time() - self._checked < 1.0:
                return False
            self._checked = time.time()
            files = self._files()
            sig = tuple((str(p), p.stat().st_mtime_ns) for p in files)
            if sig == self._sig:
                return False
            notes = []
            for i, p in enumerate(files):
                rel = p.relative_to(self.root)
                text = p.read_text(encoding="utf-8", errors="replace")
                m = re.match(r"\s*#\s+(.+)", text)
                title = m.group(1).strip() if m else p.stem
                body = re.sub(r"^\s*#\s+.+\n", "", text, count=1).strip()
                notes.append(Note(i, title, rel.parts[0] if len(rel.parts) > 1 else "general", rel.as_posix(), body,
                                  body[:EXCERPT], {w.strip().lower() for w in WIKI.findall(text)}, p.stat().st_mtime_ns))
            self.notes, self._sig = notes, sig
            self.version += 1
            return True

    def all(self) -> list[Note]:
        self.refresh()
        return self.notes

    def get(self, note_id: int) -> Note | None:
        n = self.all()
        return n[note_id] if 0 <= note_id < len(n) else None

    def graph(self) -> dict:
        """nodes: id == index in nodes. links: shared/explicit [[wikilink]] or one note mentioning the other's title."""
        notes = self.all()
        by_title = {n.title.lower(): n.id for n in notes}
        pairs = set()
        for n in notes:
            for t in n.links:
                j = by_title.get(t)
                if j is not None and j != n.id:
                    pairs.add((min(n.id, j), max(n.id, j)))
            low = n.body.lower()
            for t, j in by_title.items():
                if j != n.id and len(t) >= 5 and t in low:
                    pairs.add((min(n.id, j), max(n.id, j)))
        for a in notes:  # share a wikilink target
            for b in notes[a.id + 1:]:
                if a.links & b.links:
                    pairs.add((a.id, b.id))
        return {"version": self.version,
                "nodes": [{"id": n.id, "uid": n.uid, "label": n.title, "group": n.category, "excerpt": n.excerpt, "path": n.path} for n in notes],
                "links": [{"source": a, "target": b} for a, b in sorted(pairs)]}

    # ---- writes: ONE door for new notes -------------------------------------------------------------
    def _unique(self, folder: Path, stem: str) -> Path:
        p, k = folder / f"{stem}.md", 2
        while p.exists():
            p, k = folder / f"{stem}-{k}.md", k + 1
        return p

    def add(self, text: str, category: str = "captures", title: str | None = None, related: list[str] | None = None) -> Note:
        """Write a markdown note, re-index immediately, return it. Raises on any failure (never silent)."""
        text = text.strip()
        if not text:
            raise ValueError("empty note")
        category = slugify(category, 30) or "captures"
        title = (title or title_from(text)).strip()
        folder = self.root / category
        folder.mkdir(parents=True, exist_ok=True)
        path = self._unique(folder, slugify(title))
        rel_line = ("\n\nRelated: " + ", ".join(f"[[{t}]]" for t in related)) if related else ""
        content = f"# {title}\n\n_Captured {date.today().isoformat()}_\n\n{text}{rel_line}\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        self.refresh(force=True)
        return next(n for n in self.notes if n.path == path.relative_to(self.root).as_posix())

    def update(self, note_id: int, text: str) -> Note:
        n = self.get(note_id)
        if n is None:
            raise KeyError(note_id)
        p = self.root / n.path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text if text.lstrip().startswith("#") else f"# {n.title}\n\n{text}", encoding="utf-8")
        os.replace(tmp, p)
        self.refresh(force=True)
        return next(x for x in self.notes if x.path == n.path)

    def delete(self, note_id: int) -> str:
        """Move to notes/.trash/ with a timestamp; returns the trash path."""
        n = self.get(note_id)
        if n is None:
            raise KeyError(note_id)
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(n.path).name}"
        os.replace(self.root / n.path, dest)
        self.refresh(force=True)
        return dest.relative_to(self.root).as_posix()
