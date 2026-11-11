"""Durable JSON-safe LangGraph checkpoint persistence backed by AgentRunRow.state_payload."""
import base64
from collections import defaultdict
import logging
import threading
from langgraph.checkpoint.memory import InMemorySaver

from ..db.engine import DatabaseManager
from ..db.models import AgentRunRow

log = logging.getLogger("jarvis.checkpointer")


def _freeze_defaultdicts(obj):
    if isinstance(obj, defaultdict):
        return {k: _freeze_defaultdicts(v) for k, v in obj.items()}
    if isinstance(obj, dict):
        return {k: _freeze_defaultdicts(v) for k, v in obj.items()}
    return obj


class DBBackedCheckpointSaver(InMemorySaver):
    """InMemorySaver whose storage/writes/blobs persist across process restarts via SqlAlchemy AgentRunRow."""

    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db
        self._db_lock = threading.RLock()
        self._load_all()

    def _load_all(self) -> None:
        with self.db.sync_session() as s:
            rows = s.query(AgentRunRow).filter(AgentRunRow.state_payload.is_not(None)).all()
            for r in rows:
                p = r.state_payload or {}
                b64 = p.get("checkpoint_b64")
                kind = p.get("checkpoint_kind")
                if not (b64 and kind):
                    continue
                try:
                    raw = base64.b64decode(b64.encode("ascii"))
                    st, wr, bl = self.serde.loads_typed((kind, raw))
                    for tid, ns_map in (st or {}).items():
                        for ns, cp_map in (ns_map or {}).items():
                            for cid, tpl in (cp_map or {}).items():
                                self.storage[str(tid)][str(ns)][str(cid)] = (
                                    (str(tpl[0][0]), bytes(tpl[0][1])),
                                    (str(tpl[1][0]), bytes(tpl[1][1])),
                                    tpl[2],
                                )
                    for wk, wvals in (wr or {}).items():
                        wk_t = (str(wk[0]), str(wk[1]), str(wk[2]))
                        restored_w: dict = {}
                        for k2, val in (wvals or {}).items():
                            k2_t = (str(k2[0]), int(k2[1]))
                            restored_w[k2_t] = (str(val[0]), str(val[1]), (str(val[2][0]), bytes(val[2][1])), str(val[3]))
                        self.writes[wk_t] = restored_w
                    for bk, bval in (bl or {}).items():
                        bk_t = tuple(bk)
                        self.blobs[bk_t] = (str(bval[0]), bytes(bval[1]))
                except Exception:
                    log.exception("Failed to restore LangGraph checkpoint for thread %s", r.thread_id)

    def persist_thread(self, thread_id: str) -> None:
        """Persist checkpoint snapshot for `thread_id` into AgentRunRow.state_payload cleanly."""
        with self._db_lock:
            st = {thread_id: _freeze_defaultdicts(self.storage.get(thread_id, {}))}
            wr = {k: dict(v) for k, v in self.writes.items() if k[0] == thread_id}
            bl = {k: v for k, v in self.blobs.items() if k[0] == thread_id}
            kind, blob = self.serde.dumps_typed((st, wr, bl))
            b64 = base64.b64encode(blob).decode("ascii")
            with self.db.sync_session() as s:
                row = s.query(AgentRunRow).filter(AgentRunRow.thread_id == thread_id).first()
                if row:
                    cur = dict(row.state_payload or {})
                    cur["checkpoint_kind"] = kind
                    cur["checkpoint_b64"] = b64
                    row.state_payload = cur
                    s.commit()

    def put(self, config, checkpoint, metadata, new_versions):
        cfg = super().put(config, checkpoint, metadata, new_versions)
        tid = (config.get("configurable") or {}).get("thread_id")
        if tid:
            self.persist_thread(str(tid))
        return cfg

    def put_writes(self, config, writes, task_id, task_path=""):
        super().put_writes(config, writes, task_id, task_path)
        tid = (config.get("configurable") or {}).get("thread_id")
        if tid:
            self.persist_thread(str(tid))

    async def aput(self, config, checkpoint, metadata, new_versions):
        cfg = await super().aput(config, checkpoint, metadata, new_versions)
        tid = (config.get("configurable") or {}).get("thread_id")
        if tid:
            self.persist_thread(str(tid))
        return cfg

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await super().aput_writes(config, writes, task_id, task_path)
        tid = (config.get("configurable") or {}).get("thread_id")
        if tid:
            self.persist_thread(str(tid))
