"""The Assistant service: owns every component and runs a conversational turn (commands -> retrieval -> brain -> speech)."""
import asyncio
import datetime
import json
import logging
import re
import threading
import time
from typing import AsyncIterator

from . import commands, sysinfo
from .brain import Brain, BrainError
from .config import DATA, Settings
from .history import History
from .notes import NoteStore
from .persona import build_messages
from .retrieval import search
from .stt import Whisper
from .tts import PiperTTS, Speaker
from .voice import VoiceLoop
from .wakeword import WakeWord

log = logging.getLogger("jarvis.assistant")
MIN_SCORE = 3.0  # retrieval score below this = small talk: no notes in the prompt, no camera movement
SENT_END = re.compile(r"[.!?][\"')\]]*\s")


class EventBus:
    """Fan-out of UI events to SSE subscribers; publish() is safe from any thread."""
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def publish(self, ev: dict):
        def _put():
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    pass  # a stuck client must not stall the app
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(_put)


class Assistant:
    def __init__(self, settings: Settings, data_dir=DATA, brain: Brain | None = None):
        self.settings = settings
        self.bus = EventBus()
        self.store = NoteStore(settings.notes_path)
        self.brain = brain or Brain(settings)
        self.history = History(data_dir / "history.jsonl", settings.history_turns)
        self.tts = PiperTTS(settings.tts)
        self.speaker = Speaker(self.tts, on_state=lambda v: self.bus.publish({"type": "speaking", "on": v}))
        self.stt = Whisper(settings.stt)
        self.wake = WakeWord(settings.wake)
        self.voice = VoiceLoop(settings, self.wake, self.stt, self.speaker, self.bus.publish, self._voice_text)
        self.activity = "idle"
        self.loop: asyncio.AbstractEventLoop | None = None
        self._turn_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self.warm_status = {"stt": False, "tts": False, "brain": False}

    # ---- lifecycle -----------------------------------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.bus.loop = self.loop
        threading.Thread(target=self._warm_speech, name="warm-speech", daemon=True).start()
        self._spawn(self._watch_notes())

    async def stop(self):
        self.voice.disable()
        self.speaker.stop()
        for t in self._bg:
            t.cancel()
        await self.brain.client.aclose()

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _warm_speech(self):
        for name, comp in (("tts", self.tts), ("stt", self.stt)):
            try:
                if comp.ready:
                    comp.load()
                    self.warm_status[name] = True
            except Exception:
                log.exception("warm %s failed", name)

    def warm_brain(self):
        async def _w():
            try:
                await self.brain.warm()
                self.warm_status["brain"] = True
            except Exception as e:
                log.warning("brain warm failed: %s", e)
        self._spawn(_w())

    async def _watch_notes(self):
        v = self.store.version
        while True:
            await asyncio.sleep(3)
            if await asyncio.to_thread(self.store.refresh) and self.store.version != v:
                v = self.store.version
                self.bus.publish({"type": "graph_changed"})

    def apply_settings(self, new: Settings):
        """Live-apply changed settings (tts/wake/audio/history); brains keep their current selection."""
        self.settings.tts, self.settings.wake, self.settings.audio = new.tts, new.wake, new.audio
        self.settings.history_turns = new.history_turns
        self.history.turns = new.history_turns
        self.tts.spec = new.tts
        self.tts._voice = None  # reload on next use (voice may have changed)
        self.wake.spec = new.wake
        self.voice.s = self.settings

    # ---- turns ---------------------------------------------------------------------------------------
    def _voice_text(self, text: str):
        """Called from the voice thread with a transcribed command."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._voice_turn(text), self.loop)

    async def _voice_turn(self, text: str):
        self.bus.publish({"type": "user", "text": text})
        async for ev in self.stream_turn(text, speak=True, source="voice"):
            self.bus.publish(ev)

    def _context(self, text: str):
        q = text
        if len(text.split()) < 4 and self.history.items:  # short follow-up: borrow the previous question's keywords
            prev = next((m["content"] for m in reversed(self.history.items) if m["role"] == "user"), "")
            q = f"{prev} {text}"
        hits = [(n, s) for n, s in search(self.store.all(), q, self.settings.context_notes) if s >= MIN_SCORE]
        return hits

    async def stream_turn(self, text: str, speak: bool = False, source: str = "text") -> AsyncIterator[dict]:
        """Yields {"type": "meta"|"token"|"done"|"error", ...}. The only place a turn is executed."""
        text = text.strip()
        async with self._turn_lock:
            self.speaker.stop()  # new input interrupts any ongoing speech
            self._set_activity("thinking")
            t0 = time.time()
            try:
                cmd = await commands.route(self, text)
                if cmd:
                    reply = cmd["reply"]
                    yield {"type": "meta", "command": cmd["name"], "nodes": cmd["nodes"], "model": None}
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak and cmd.get("speak", True):
                        self.speaker.say(reply)
                    yield {"type": "done", "answer": reply, "nodes": cmd["nodes"],
                           "paths": cmd.get("paths") or ([cmd["note"]] if cmd.get("note") else []), "ui": cmd.get("ui"),
                           "command": cmd["name"], "ok": cmd.get("ok", True), "seconds": round(time.time() - t0, 2)}
                    return
                hits = self._context(text)
                nodes = [n.id for n, _ in hits]
                msgs = build_messages(self.history.window(), text, [(n.title, n.excerpt[:self.settings.context_chars]) for n, _ in hits], datetime.datetime.now().strftime("%A %d %B %Y, %H:%M"))
                yield {"type": "meta", "nodes": nodes, "model": self.brain.spec.model, "sources": [n.title for n, _ in hits]}
                answer, buf = "", ""
                async for piece in self.brain.stream(msgs):
                    answer += piece
                    yield {"type": "token", "text": piece}
                    if speak:
                        buf += piece
                        m = None
                        for m in SENT_END.finditer(buf):
                            pass
                        if m and len(buf[:m.end()]) >= 100:  # speak whole sentences, in chunks of useful size
                            self.speaker.say(buf[:m.end()])
                            buf = buf[m.end():]
                if speak and buf.strip():
                    self.speaker.say(buf)
                answer = answer.strip()
                if not answer:
                    raise BrainError("The model returned an empty reply.")
                self.history.add("user", text)
                self.history.add("assistant", answer)
                st = self.brain.last_stats
                yield {"type": "done", "answer": answer, "nodes": nodes, "paths": [n.path for n, _ in hits], "command": None, "ok": True, "seconds": round(time.time() - t0, 2),
                       "ttft": round(st.get("ttft") or 0, 2), "tps": round(st.get("tps", 0), 1), "model": st.get("model"),
                       "truncated": st.get("truncated", False)}
            except BrainError as e:
                if speak:
                    self.speaker.say("I'm afraid I can't reach my language model, sir.")
                yield {"type": "error", "message": str(e)}
            finally:
                self._set_activity("speaking" if self.speaker.speaking else "idle")

    async def chat(self, text: str, speak: bool = False) -> dict:
        last = {}
        async for ev in self.stream_turn(text, speak):
            last = ev
        if last.get("type") == "error":
            raise BrainError(last["message"])
        return last

    def _set_activity(self, a: str):
        if a != self.activity:
            self.activity = a
            self.bus.publish({"type": "activity", "state": a})

    # ---- status --------------------------------------------------------------------------------------
    async def status(self) -> dict:
        h = await self.brain.health()
        return {"ollama": h, "brain": {"key": self.brain.current, "model": self.brain.spec.model, "thinking": self.brain.spec.thinking,
                                         "num_ctx": self.brain.spec.num_ctx},
                "voice": {"state": self.voice.state, "error": self.voice.last_error, "wake_enabled": self.settings.wake.enabled,
                          "speaking": self.speaker.speaking},
                "ready": {"tts": self.tts.ready, "stt": self.stt.ready, "wake": self.wake.ready, **{f"{k}_warm": v for k, v in self.warm_status.items()}},
                "notes": len(self.store.all()), "activity": self.activity,
                "tts": {"enabled": self.settings.tts.enabled, "voice": self.settings.tts.voice},
                "offline_enforced": self.settings.offline_enforced,
                "system": {"ram": sysinfo.ram(), "gpu": await asyncio.to_thread(sysinfo.gpu), "cpu_pct": sysinfo.cpu(),
                           "audio": await asyncio.to_thread(sysinfo.audio)}}
