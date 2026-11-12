"""The Assistant service: owns every component and runs a conversational turn (commands -> retrieval -> brain/web tools -> speech)."""
import asyncio
import datetime
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import AsyncIterator

from . import audit, commands, graph_live, sysinfo
from .actions import ActionExecutor
from .aec import EchoReferenceBuffer, NLMSEchoCanceller
from .agents.orchestrator import AgentOrchestrator
from .brain import Brain, BrainError, ToolsUnsupportedError
from .config import DATA, Settings
from .core.event_bus import EventBus
from .filesystem import FilesystemPermissionError, SafeFilesystem
from .filesystem_access import FilesystemSecurityError, WRITE_OPS
from .gateway.interface import GatewayRequest
from .history import History
from .learning.evaluation import ModelEvaluator
from .learning.feedback import FeedbackStore
from .learning.knowledge_pipeline import KnowledgePipeline
from .learning.training.dataset_builder import DatasetBuilder
from .learning.training.dataset_cleaner import DatasetCleaner
from .learning.training.dataset_manager import DatasetManager
from .learning.training.dataset_sources import DatasetSourceCatalog
from .learning.training.dataset_validator import DatasetValidator
from .learning.training.model_registry import ModelRegistry
from .learning.training.rollback import rollback_active_model
from .integrations import git_worktree
from .learning.training.training_audit import TrainingAuditLog
from .learning.training.training_config import TrainingJobConfig
from .learning.training.training_manager import TrainingManager
from .learning.training.training_scheduler import TrainingScheduler
from .memory_store import PersonalMemoryStore
from .notes import NoteStore
from .persona import build_messages
from .retrieval import search
from .search import SearchError, WebSearchService
from .stt import Whisper
from .translator import Translator
from .tts import PiperTTS, Speaker, StreamingSpeechBuffer
from .voice import VoiceLoop
from .wakeword import WakeWord

log = logging.getLogger("jarvis.assistant")
MIN_SCORE = 3.0  # retrieval score below this = small talk: no notes in the prompt, no camera movement
SENT_END = re.compile(r"[.!?][\"')\]]*\s")
WEB_INTENT_RE = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|look\s+up\s+online|google|latest|current|today'?s|"
    r"news\b|weather\b|forecast\b|price\s+of|stock\s+price|exchange\s+rate|who\s+won|release\s+date|what\s+version)\b",
    re.I,
)
WEB_QUERY_STRIP = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:search\s+(?:the\s+)?(?:web|internet|online)\s+(?:for|about)?|look\s+up\s+online|google)\s*",
    re.I,
)


def needs_web_intent(text: str, has_notes: bool = False) -> bool:
    """Deterministic fallback intent check for live/current web information."""
    if commands.WEB_EXPLICIT.match(text):
        return True
    if not has_notes and WEB_INTENT_RE.search(text):
        return True
    return False


def extract_web_query(text: str) -> str:
    q = WEB_QUERY_STRIP.sub("", (text or "").strip()).strip(" ?!.")
    return q or text.strip()


def _web_tools(allow_page_fetch: bool) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the internet for current information, news, software versions, weather, prices, or external facts.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Concise web search query"}},
                    "required": ["query"],
                },
            },
        }
    ]
    if allow_page_fetch:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "fetch_webpage",
                    "description": "Read readable text from a specific https:// webpage URL returned by web_search.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string", "description": "An https:// URL from search results"}},
                        "required": ["url"],
                    },
                },
            }
        )
    return tools


class Assistant:
    def __init__(self, settings: Settings, data_dir=DATA, brain: Brain | None = None, search_service: WebSearchService | None = None):
        self.settings = settings
        self.data_dir = Path(data_dir)
        audit.set_online_enabled(settings.internet.enabled)
        self.bus = EventBus()
        self.store = NoteStore(settings.notes_path)
        self.memory_store = PersonalMemoryStore(self.data_dir / "memory" / "records.jsonl", settings.memory)
        self.filesystem = SafeFilesystem(settings.filesystem)
        self.actions = ActionExecutor(settings.actions, self.filesystem)
        self.brain = brain or Brain(settings)
        self.history = History(self.data_dir / "history.jsonl", settings.history_turns)
        self.conversation_refs: set[str] = set()  # graph ids the current conversation retrieved (live graph links)
        self.used_tools: set[str] = set()  # real capability/web tools invoked during this session
        self.os_state = "Idle"

        online_fn = lambda: bool(self.settings.internet.enabled and audit.is_online_enabled())  # noqa: E731
        self.knowledge = KnowledgePipeline(settings.learning, data_dir=self.data_dir / "learning" / "knowledge", is_online_fn=online_fn)
        self.feedback = FeedbackStore(self.data_dir / "learning" / "feedback" / "records.jsonl")
        self.learning_audit = TrainingAuditLog(self.data_dir / "learning" / "training" / "audit.jsonl")
        self.dataset_sources = DatasetSourceCatalog(settings.learning, is_online_fn=online_fn)
        self.dataset_manager = DatasetManager(
            settings.learning,
            training_dir=self.data_dir / "learning" / "training",
            is_online_fn=online_fn,
            audit_log=self.learning_audit,
        )
        self.dataset_validator = DatasetValidator(settings.learning, quarantine_dir=self.data_dir / "learning" / "training" / "quarantine")
        self.dataset_cleaner = DatasetCleaner(cleaned_dir=self.data_dir / "learning" / "training" / "cleaned")
        self.dataset_builder = DatasetBuilder(prepared_dir=self.data_dir / "learning" / "training" / "prepared")
        self.training_manager = TrainingManager(
            settings.learning,
            jobs_dir=self.data_dir / "learning" / "training" / "jobs",
            prepared_dir=self.data_dir / "learning" / "training" / "prepared",
            audit_log=self.learning_audit,
        )
        self.training_scheduler = TrainingScheduler(
            settings.learning,
            state_path=self.data_dir / "learning" / "training" / "scheduler_state.json",
            audit_log=self.learning_audit,
        )
        self.evaluator = ModelEvaluator(eval_dir=self.data_dir / "learning" / "evaluations")
        self.registry = ModelRegistry(
            settings.learning,
            registry_path=self.data_dir / "learning" / "registry.json",
            audit_log=self.learning_audit,
        )
        self.registry.ensure_baseline(self.brain.spec.model)
        self.tts = PiperTTS(settings.tts)
        self.echo_ref = EchoReferenceBuffer()
        self.aec = NLMSEchoCanceller()
        self.speaker = Speaker(
            self.tts,
            on_state=self._on_speaker_state,
            on_error=lambda msg: self.bus.publish({"type": "error", "message": msg}),
            on_status=lambda st: self.bus.publish({"type": "tts_status", **st}),
            echo_ref=self.echo_ref,
        )
        self.stt = Whisper(settings.stt)
        self.translator = Translator(self.stt, self.brain)
        self.search = search_service or WebSearchService(settings.internet)
        self.wake = WakeWord(settings.wake)
        self.voice = VoiceLoop(
            settings,
            self.wake,
            self.stt,
            self.speaker,
            self.bus.publish,
            self._voice_text,
            on_result=self._voice_result,
            on_utterance=self._schedule_voice_queue,
            on_interrupt=self._interrupt_voice_turn,
            aec=self.aec,
            echo_ref=self.echo_ref,
        )
        self.activity = "idle"
        self.loop: asyncio.AbstractEventLoop | None = None
        self._turn_lock = asyncio.Lock()
        self._voice_queue_lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()
        self._active_turn_cancel: asyncio.Event | None = None
        self._voice_task: asyncio.Task | None = None
        self._last_utt_id = 0
        self._bg: set[asyncio.Task] = set()
        self.warm_status = {"stt": False, "tts": False, "brain": False}
        self.orchestrator = AgentOrchestrator(settings, bus=self.bus, brain=self.brain, data_dir=self.data_dir)
        self.knowledge.vector_store = self.orchestrator.memory.vectors

    # ---- lifecycle -----------------------------------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.bus.loop = self.loop
        threading.Thread(target=self._warm_speech, name="warm-speech", daemon=True).start()
        self._spawn(self._watch_notes())
        self._spawn(self._watch_knowledge())
        self._spawn(self._watch_reminders())
        self._spawn(self._watch_memory_expiry())

    async def _watch_memory_expiry(self):
        """Scheduled cleanup purging expired memories and vectors atomically."""
        while True:
            try:
                res = await asyncio.to_thread(self.orchestrator.memory.purge_expired)
                if res.get("sql_purged") or res.get("vectors_purged"):
                    self.bus.publish({"type": "memory_purged", **res})
            except Exception as e:
                log.debug("memory expiry cleanup skipped: %s", e)
            await asyncio.sleep(60)

    async def _watch_reminders(self):
        """Poll scheduled reminders without touching external networks."""
        while True:
            try:
                due = await asyncio.to_thread(self.orchestrator.reminders.poll_due)
                for rem in due:
                    if self.settings.tts.enabled and "voice" in (rem.get("channel") or ""):
                        self.speaker.say(f"Reminder: {rem['title']}")
            except Exception as e:
                log.debug("reminder poll skipped: %s", e)
            await asyncio.sleep(15)

    async def _watch_knowledge(self):
        """Periodically sync configured local (and if online, remote) knowledge sources without weight modification."""
        while True:
            try:
                if self.settings.learning.enabled and self.settings.learning.automatic_knowledge_updates:
                    if self.knowledge.list_sources():
                        res = await self.knowledge.sync()
                        if res.get("new_documents_found"):
                            self.bus.publish({"type": "knowledge_updated", **res})
            except Exception as e:
                log.debug("periodic knowledge check skipped: %s", e)
            interval_s = max(300, int(self.settings.learning.update_interval_hours or 24) * 3600)
            await asyncio.sleep(interval_s)

    async def stop(self):
        self.voice.disable()
        self.speaker.stop()
        for t in self._bg:
            t.cancel()
        await self.brain.client.aclose()
        await self.orchestrator.db.close()

    def _on_speaker_state(self, speaking: bool):
        self.bus.publish({"type": "speaking", "on": speaking})
        self._set_os_state("Speaking" if speaking else "Idle", source="tts")
        self.voice.on_speaker_state(speaking)

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
        """Live-apply changed settings across voice, internet, providers, gateway, filesystem, preferences, and orchestrator."""
        prev_voice_mode = self.settings.audio.voice_mode
        self.settings.tts = new.tts
        self.settings.stt = new.stt
        self.settings.translation = new.translation
        self.settings.wake = new.wake
        self.settings.audio = new.audio
        self.settings.internet = new.internet
        self.settings.memory = new.memory
        self.settings.filesystem = new.filesystem
        self.settings.actions = new.actions
        self.settings.providers = new.providers
        self.settings.gateway = new.gateway
        self.settings.learning = new.learning
        self.settings.preferences = new.preferences
        self.settings.boot_brain = new.boot_brain
        self.settings.offline_enforced = new.offline_enforced
        self.settings.history_turns = new.history_turns
        self.history.turns = new.history_turns
        self.tts.spec = new.tts
        self.tts._voice = None  # reload on next use (voice may have changed)
        self.stt.spec = new.stt
        self.warm_status["stt"] = self.stt.ready
        self.wake.spec = new.wake
        self.voice.s = self.settings
        if getattr(self, "orchestrator", None) is not None:
            self.orchestrator.settings = self.settings
            if getattr(self.orchestrator, "fs_access", None) is not None:
                self.orchestrator.fs_access.settings = self.settings
            if getattr(self.orchestrator, "gateway", None) is not None:
                self.orchestrator.gateway._fallback_settings = self.settings
            if getattr(self.orchestrator, "decision", None) is not None:
                self.orchestrator.decision.refresh_settings(self.settings)
        if new.audio.voice_mode != prev_voice_mode:
            self.voice.set_mode(new.audio.voice_mode)
        else:
            self.voice.mode = new.audio.voice_mode
        self.search.spec = new.internet
        self.search.status_state = "offline" if not new.internet.enabled else "ready"
        audit.set_online_enabled(new.internet.enabled)
        self.settings.memory = new.memory
        self.memory_store.spec = new.memory
        self.settings.filesystem = new.filesystem
        self.filesystem.spec = new.filesystem
        self.settings.actions = new.actions
        self.actions.spec = new.actions
        self.settings.providers = new.providers
        self.settings.learning = new.learning
        self.knowledge.spec = new.learning
        self.dataset_sources.spec = new.learning
        self.dataset_manager.spec = new.learning
        self.dataset_validator.spec = new.learning
        self.training_manager.spec = new.learning
        self.training_scheduler.spec = new.learning
        self.registry.spec = new.learning
        if hasattr(self, "orchestrator"):
            self.orchestrator.settings = new
            self.orchestrator.decision.refresh_settings(new)

    # ---- turns ---------------------------------------------------------------------------------------
    def _schedule_voice_queue(self):
        """Called from VoiceLoop thread when an utterance is placed in the bounded queue."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._ensure_voice_worker)

    def _ensure_voice_worker(self):
        if self._voice_task is None or self._voice_task.done():
            self._voice_task = self._spawn(self._drain_voice_queue())

    def _interrupt_voice_turn(self):
        """Called from VoiceLoop barge-in or API interrupt to cancel ongoing LLM generation immediately."""
        def _trigger():
            self._cancel_event.set()
            if self._active_turn_cancel is not None:
                self._active_turn_cancel.set()
            self.actions.tasks.cancel_active()
            self.orchestrator.cancel_active_runs(reason="barge_in")

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(_trigger)
        else:
            _trigger()

    async def interrupt_all(self, reason: str = "manual_interrupt") -> dict:
        """Stop ongoing TTS speech, cancel active LLM turns/tasks/workflows, and reset state safely."""
        self.speaker.stop()
        self._cancel_event.set()
        if self._active_turn_cancel is not None:
            self._active_turn_cancel.set()
        cancelled_tasks = self.actions.tasks.cancel_active()
        cancelled_runs = self.orchestrator.cancel_active_runs(reason=reason)
        self.voice.interrupt(reason=reason)
        next_state = "Listening" if self.voice.mic_active else "Idle"
        self._set_activity("idle")
        self._set_os_state(next_state, source="voice" if self.voice.mic_active else "system", detail=reason)
        return {
            "ok": True,
            "reason": reason,
            "cancelled_tasks": cancelled_tasks,
            "cancelled_runs": cancelled_runs,
            "state": self.voice.state,
            "audio_state": self.voice.audio_state.value,
            "mode": self.voice.mode,
            "mic_active": self.voice.mic_active,
        }

    async def _drain_voice_queue(self):
        import queue
        async with self._voice_queue_lock:
            while True:
                try:
                    utt_id, audio_f32 = self.voice.utterance_queue.get_nowait()
                except queue.Empty:
                    break
                if utt_id <= self._last_utt_id:
                    continue
                self._last_utt_id = utt_id
                self._cancel_event.clear()
                was_interrupted = False
                try:
                    was_interrupted = await self._run_voice_utterance(utt_id, audio_f32)
                except asyncio.CancelledError:
                    was_interrupted = True
                    raise
                except Exception as e:
                    log.exception("voice turn crashed")
                    self.bus.publish({"type": "error", "message": f"voice turn failed: {e}"})
                finally:
                    self.voice.complete_turn(utt_id, was_interrupted=was_interrupted or self._cancel_event.is_set())

    async def _run_voice_utterance(self, utt_id: int, audio_f32) -> bool:
        try:
            stt_res = await asyncio.to_thread(self.stt.transcribe, audio_f32)
        except Exception as e:
            log.exception("transcribe failed")
            self.bus.publish({"type": "error", "message": f"transcription failed: {e}"})
            return False

        if self._cancel_event.is_set():
            return True

        text = (stt_res.get("text") or "").strip()
        self.bus.publish({"type": "transcript", **stt_res, "text": text})
        if not text:
            return False
        graph_live.publish_os_event(
            self.bus,
            "assistant.transcript.partial",
            legacy_type="transcript_partial",
            source_department="assistant",
            payload={"text": text, "language": stt_res.get("language", "unknown")},
            text=text,
        )

        rec = await self.translator.process_stt(stt_res, self.settings.translation, audio=audio_f32)
        self.bus.publish({"type": "transcript_record", **rec.to_dict()})
        use_translated = bool(rec.translated_text and rec.target_language == "en" and rec.detected_language != "en")
        prompt_text = ((rec.translated_text if use_translated else rec.corrected_transcript) or "").strip()
        if not prompt_text or self._cancel_event.is_set():
            return self._cancel_event.is_set()

        graph_live.publish_os_event(
            self.bus,
            "assistant.transcript.final",
            legacy_type="transcript_final",
            source_department="assistant",
            payload={"transcript_id": rec.id, "text": prompt_text, "language": rec.detected_language},
            text=prompt_text,
            transcript_id=rec.id,
        )
        self.bus.publish({"type": "user", "text": prompt_text, "transcript_id": rec.id})
        self.speaker.last_error = None
        self.voice.mark_generating(utt_id)
        async for ev in self.stream_turn(prompt_text, speak=True, source="voice", cancel_event=self._cancel_event):
            self.bus.publish(ev)
            if self.speaker.speaking:
                self.voice.mark_speaking(utt_id)
            if ev.get("type") == "interrupted":
                return True

        if self._cancel_event.is_set():
            return True

        # Wait until Piper playback + queued chunks actually finish before VoiceLoop reopens mic
        if self.settings.tts.enabled and (self.speaker.speaking or self.speaker._pending > 0):
            await asyncio.to_thread(self.speaker.wait_idle, 120)
        return self._cancel_event.is_set()

    def _voice_text(self, text: str):
        """Called from standalone VoiceLoop when wired with a plain transcript string."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._voice_turn({"text": text}), self.loop)

    def _voice_result(self, stt_result: dict):
        """Called from standalone VoiceLoop with rich STT metadata."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._voice_turn(stt_result), self.loop)

    async def _voice_turn(self, stt_result: dict):
        audio_id = stt_result.get("audio_id")
        audio = self.stt.audio_store.get(str(audio_id)) if audio_id else None
        rec = await self.translator.process_stt(stt_result, self.settings.translation, audio=audio)
        self.bus.publish({"type": "transcript_record", **rec.to_dict()})
        use_translated = bool(rec.translated_text and rec.target_language == "en" and rec.detected_language != "en")
        prompt_text = ((rec.translated_text if use_translated else rec.corrected_transcript) or "").strip()
        if not prompt_text:
            return
        self.bus.publish({"type": "user", "text": prompt_text, "transcript_id": rec.id})
        async for ev in self.stream_turn(prompt_text, speak=True, source="voice"):
            self.bus.publish(ev)

    def _gather_context(self, text: str) -> dict:
        """Collect provenance-separated personal memory, history, feedback, notes, and indexed knowledge."""
        q = text
        if len(text.split()) < 4 and self.history.items:  # short follow-up: borrow previous question keywords
            prev = next((m["content"] for m in reversed(self.history.items) if m["role"] == "user"), "")
            q = f"{prev} {text}"
        hits = [(n, s) for n, s in search(self.store.all(), q, self.settings.context_notes) if s >= MIN_SCORE]

        mem_hits = [(r, s) for r, s in self.memory_store.search(text, limit=3) if s >= 1.5]
        memories_ctx = [(r.category, r.text) for r, _ in mem_hits]

        win = self.history.window()
        win_contents = {m["content"] for m in win}
        disk_hits = [h for h in self.history.search_disk(text, limit=4) if h.get("content") not in win_contents][:2]
        history_ctx = [f"{h['role']}: {h['content'][:180]}" for h in disk_hits]

        fb_hits = self.feedback.retrieve_lessons(text, limit=2)
        feedback_ctx = [f"{f['topic']}: {f['corrected_response'][:220]}" for f in fb_hits]

        kb_raw = [k for k in self.knowledge.search(text, k=2) if float(k.get("score") or 0) >= 2.0]
        knowledge_ctx = [(f"{k['title']} [{k['trust']}]", k["content"][:320]) for k in kb_raw]

        return {
            "hits": hits,
            "nodes": [n.id for n, _ in hits],
            "notes_ctx": [(n.title, n.excerpt[:self.settings.context_chars]) for n, _ in hits],
            "memories_ctx": memories_ctx,
            "memory_ids": [r.id for r, _ in mem_hits],
            "history_ctx": history_ctx,
            "feedback_ctx": feedback_ctx,
            "knowledge_ctx": knowledge_ctx,
            "knowledge_sources": [{"source_id": k["source_id"], "title": k["title"], "trust": k["trust"]} for k in kb_raw],
        }

    def _context(self, text: str):
        q = text
        if len(text.split()) < 4 and self.history.items:
            prev = next((m["content"] for m in reversed(self.history.items) if m["role"] == "user"), "")
            q = f"{prev} {text}"
        return [(n, s) for n, s in search(self.store.all(), q, self.settings.context_notes) if s >= MIN_SCORE]

    async def _stream_llm_with_tts(
        self,
        msgs: list[dict],
        *,
        speak: bool,
        cancel_event: asyncio.Event | None,
        answer_out: list[str],
        role: str = "fast",
    ) -> AsyncIterator[dict]:
        """Single reusable token + sentence-chunked TTS streamer via local brain or ModelGateway."""
        chunker = StreamingSpeechBuffer()
        buf = ""
        gw = getattr(self.orchestrator, "gateway", None)
        active_prov = getattr(self.settings.providers, "active_provider", "ollama") or "ollama"
        use_gateway = False
        primary_key = self.brain.current
        fallbacks: list[str] = []
        if gw is not None and active_prov != "ollama":
            primary_key, fallbacks = gw.select_model_for_task(role)
            use_gateway = True

        if use_gateway and gw is not None:
            gw_req = GatewayRequest(
                model_key=primary_key,
                messages=msgs,
                temperature=0.4,
                max_tokens=int(getattr(self.brain.spec, "max_tokens", 1024) or 1024),
                decision_layer=f"assistant:{role}",
                project_privacy_level="private",
                cancel_event=cancel_event,
                timeout_s=float(self.settings.gateway.request_timeout_s),
            )
            async for ev in gw.stream_with_fallback(gw_req, fallback_models=fallbacks):
                if cancel_event and cancel_event.is_set():
                    self.speaker.stop()
                    answer_out.append(buf)
                    yield {"type": "interrupted"}
                    return
                if ev.type == "token" and ev.text:
                    piece = ev.text
                    buf += piece
                    yield {"type": "token", "text": piece}
                    if speak:
                        for sentence in chunker.feed(piece):
                            self.speaker.say_chunk(sentence, precleaned=True)
        else:
            async for piece in self.brain.stream(msgs):
                if cancel_event and cancel_event.is_set():
                    self.speaker.stop()
                    answer_out.append(buf)
                    yield {"type": "interrupted"}
                    return
                buf += piece
                yield {"type": "token", "text": piece}
                if speak:
                    for sentence in chunker.feed(piece):
                        self.speaker.say_chunk(sentence, precleaned=True)
        if speak:
            for sentence in chunker.flush():
                self.speaker.say_chunk(sentence, precleaned=True)
        answer_out.append(buf)

    async def _brain_complete(self, msgs: list[dict]) -> str:
        parts: list[str] = []
        async for piece in self.brain.stream(msgs):
            parts.append(piece)
        return "".join(parts).strip()

    # ---- learning & training helpers -----------------------------------------------------------------
    def learning_overview(self) -> dict:
        sources = self.knowledge.list_sources()
        builds = self.dataset_builder.list_builds()
        jobs = self.training_manager.list_jobs()
        evals = self.evaluator.list_reports(limit=10)
        ollama_ps: list[dict] = []
        return {
            "enabled": self.settings.learning.enabled,
            "policy": self.settings.learning.model_dump(),
            "knowledge": self.knowledge.freshness(),
            "knowledge_sources_count": len(sources),
            "knowledge_sources": sources,
            "feedback": self.feedback.summary(),
            "datasets": self.dataset_manager.list_datasets(),
            "dataset_builds": builds,
            "jobs": jobs,
            "evaluations": evals,
            "registry": self.registry.summary(),
            "hardware": self.training_manager.hardware_profile(ollama_ps=ollama_ps),
            "scheduler": self.training_scheduler.get_status_snapshot(self.feedback, self.dataset_builder),
            "audit_recent": self.learning_audit.recent(limit=25),
        }

    def prepare_training_dataset_from_feedback(self, *, target_format: str = "messages") -> dict:
        snap = self.dataset_manager.import_approved_feedback(self.feedback)
        report, valid_recs = self.dataset_validator.validate_file(
            Path(snap["file"]),
            dataset_id="approved-feedback",
            version=snap["version"],
            license_name=snap["license"],
        )
        self.dataset_manager.update_stage_report("approved-feedback", snap["version"], "validation", report.to_dict())
        if not report.approved:
            raise ValueError("; ".join(report.reasons) or "Dataset failed validation")
        clean_rep, cleaned = self.dataset_cleaner.clean(valid_recs, dataset_id="approved-feedback", version=snap["version"])
        self.dataset_manager.update_stage_report("approved-feedback", snap["version"], "cleaning", clean_rep.to_dict())
        build_id = f"build-feedback-v{snap['version']}-{int(time.time())}"
        built = self.dataset_builder.build(
            cleaned,
            build_id=build_id,
            dataset_ids=[f"approved-feedback:v{snap['version']}"],
            target_format=target_format,
        )
        return {"snapshot": snap, "validation": report.to_dict(), "cleaning": clean_rep.to_dict(), "build": built.to_dict()}

    async def request_training_job(self, *, dataset_build_id: str, base_model: str | None = None, epochs: int = 1, max_seq_length: int = 512) -> dict:
        h = await self.brain.health()
        cfg = TrainingJobConfig(
            base_model=base_model or self.brain.spec.model,
            dataset_build_id=dataset_build_id,
            epochs=epochs,
            max_seq_length=max_seq_length,
        )
        return self.training_manager.create_job(cfg, ollama_ps=h.get("loaded") or [])

    async def run_model_evaluation(self, candidate_version_id: str | None = None) -> dict:
        reg = self.registry.summary()
        base_ver = reg["versions"][-1] if reg["versions"] else None
        base_model = (base_ver or {}).get("base_model") or self.brain.spec.model
        cand_rec = None
        if candidate_version_id:
            cand_rec = next((v for v in reg["versions"] if v["version_id"] == candidate_version_id), None)
        report = await self.evaluator.evaluate(
            baseline_model=base_model,
            baseline_fn=self._brain_complete,
            candidate_model=cand_rec["ollama_tag"] if cand_rec else None,
            candidate_fn=self._brain_complete if cand_rec else None,
        )
        rep_dict = report.to_dict()
        if cand_rec:
            self.registry.attach_evaluation(
                cand_rec["version_id"],
                evaluation_id=report.eval_id,
                score=report.candidate_score if report.candidate_score is not None else report.baseline_score,
                passed=report.passed,
                reason="; ".join(report.reasons),
            )
        self.learning_audit.record("evaluation_completed", details={"eval_id": report.eval_id, "decision": report.decision, "baseline_score": report.baseline_score})
        return rep_dict

    def rollback_model(self, target_version_id: str | None = None) -> dict:
        return rollback_active_model(self.registry, target_version_id=target_version_id, audit_log=self.learning_audit)

    async def _execute_tool(self, call: dict) -> tuple[str, list[dict]]:
        """Execute ONLY approved backend web tools; never shell, Python, or unapproved URLs."""
        name = call.get("name", "")
        args = call.get("arguments") or {}
        if name == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return json.dumps({"error": "empty query"}), []
            try:
                res = await self.search.search(query)
                results = res["results"]
                return json.dumps({"query": res["query"], "provider": res["provider"], "results": results}, ensure_ascii=False), results
            except SearchError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False), []
        if name == "fetch_webpage":
            url = str(args.get("url") or "").strip()
            try:
                page = await self.search.fetch_page(url)
                return json.dumps(page, ensure_ascii=False), [{"title": page["title"], "url": page["url"], "snippet": page["content"][:220], "provider": "webpage"}]
            except SearchError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False), []
        return json.dumps({"error": f"tool {name!r} is not permitted"}), []

    @staticmethod
    def _match_ui_command(text: str) -> dict | None:
        """Recognize single-screen voice/text commands that open/close contextual drawers without leaving Assistant OS."""
        m = re.match(
            r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(open|show|display|focus|hide|close)\s+(?:the\s+)?"
            r"(brain\s*explorer|explorer|approvals?|workforce|agent\s*runs?|memory(?:\s+inspector)?|"
            r"system(?:\s+monitor)?|voice|learning|knowledge\s+graph|graph\s+view|"
            r"inspector|drawer|panel)(?:\s+(?:drawer|panel|controls?|settings|view|tab))?\W*$",
            text or "",
            re.I,
        )
        if not m:
            return None
        verb = m.group(1).lower()
        target = re.sub(r"\s+", " ", m.group(2).lower().strip())
        if verb in ("hide", "close"):
            return {"command": "ui_drawer", "reply": "Closing contextual panel.", "ui": {"open_drawer": None}}
        mapping = {
            "brain explorer": "explorer",
            "explorer": "explorer",
            "approval": "approvals",
            "approvals": "approvals",
            "workforce": "workforce",
            "agent run": "workforce",
            "agent runs": "workforce",
            "memory": "memory",
            "memory inspector": "memory",
            "system": "system",
            "system monitor": "system",
            "voice": "voice",
            "learning": "learning",
            "inspector": "explorer",
            "drawer": "explorer",
            "panel": "explorer",
        }
        if target in ("knowledge graph", "graph view"):
            return {"command": "ui_focus", "reply": "Focusing the live knowledge graph.", "ui": {"open_drawer": None, "focus": "graph"}}
        drawer = mapping.get(target)
        if not drawer:
            return None
        return {"command": "ui_drawer", "reply": f"Opening {target} drawer alongside the assistant.", "ui": {"open_drawer": drawer}}

    @staticmethod
    def _extract_fs_query(text: str) -> tuple[bool, str]:
        low = (text or "").lower()
        wants_inspect = bool(re.search(r"\b(?:inspect|structure|tree|directories|folders?|what\s+files)\b", low))
        m = re.search(r"(?:named|matching|for|about|called)\s+([a-z0-9_.\-\/\\]+)", text or "", re.I)
        if m:
            return wants_inspect, m.group(1).strip(" .?!\"'")
        tokens = [w.strip(" .?!\"'") for w in (text or "").split() if "." in w or "_" in w or "/" in w]
        return wants_inspect, (tokens[-1] if tokens else "")

    @staticmethod
    def _parse_host_fs_request(text: str) -> dict:
        """Extract canonical host folder/file target, operation, search query, and extensions from user request."""
        raw = (text or "").strip()
        low = raw.lower()
        explicit_path = ""

        for pat in (
            r'(\\\\[^\s"\']+)',
            r'"([A-Za-z]:\\[^"]+|/[^"]+)"',
            r"'([A-Za-z]:\\[^']+|/[^']+)'",
            r'([A-Za-z]:\\[^\s"\'?,;]+)',
            r'([A-Za-z]:/(?:[^\s"\'?,;]+))',
            r'(~[\\/][^\s"\'?,;]+)',
            r'(\.\.[\\/][^\s"\'?,;]+|[A-Za-z0-9_.-]+(?:[\\/]\.\.)+[^\s"\'?,;]*)',
        ):
            m = re.search(pat, raw)
            if m:
                explicit_path = m.group(1).strip()
                break

        # Detect drive root alone like "C:\" or "List C:\"
        if not explicit_path:
            dm = re.search(r'\b([A-Za-z]:\\?)\b', raw)
            if dm and ":" in dm.group(1):
                val = dm.group(1)
                explicit_path = val if val.endswith("\\") else val + "\\"

        alias_path = ""
        unspecified_folder = False
        if not explicit_path:
            if re.search(r"\b(?:in\s+(?:the\s+|my\s+)?documents(?:\s+folder)?|my\s+documents\b|documents\s+folder\b)", low):
                alias_path = str(Path.home() / "Documents")
            elif re.search(r"\b(?:in\s+(?:the\s+|my\s+)?downloads(?:\s+folder)?|my\s+downloads\b|downloads\s+folder\b)", low):
                alias_path = str(Path.home() / "Downloads")
            elif re.search(r"\b(?:in\s+(?:the\s+|my\s+)?desktop(?:\s+folder)?|my\s+desktop\b|desktop\s+folder\b)", low):
                alias_path = str(Path.home() / "Desktop")
            elif re.search(r"\b(?:in\s+(?:the\s+|my\s+)?pictures(?:\s+folder)?|my\s+pictures\b|pictures\s+folder\b)", low):
                alias_path = str(Path.home() / "Pictures")
            elif "workspace" not in low:
                unspecified_folder = True

        target = explicit_path or alias_path or str(Path.home() / "Documents")
        is_workspace_only = ("workspace" in low) and not explicit_path and not alias_path

        # Operation classification strictly separated across read / write / execute / delete
        if re.search(r"\b(?:delete|remove|unlink|erase)\b", low):
            operations = ["delete_file"]
        elif re.search(r"\b(?:edit|modify|update|append)\b", low):
            operations = ["edit_file", "write_file"]
        elif re.search(r"\b(?:create|write|save|generate|build|prepare)\b", low) and re.search(
            r"\b(?:report|summary|file|document|note|markdown|\.md|\.txt|\.json|\.csv)\b", low
        ):
            operations = ["create_file", "write_file"]
        elif re.search(r"\b(?:read|cat|show\s+contents|open)\b", low) and not re.search(r"\b(?:search|find|list)\b", low):
            operations = ["read_file", "read_metadata"]
        elif re.search(r"\b(?:list|browse|what\s+files\s+are\s+in)\b", low) and not re.search(r"\b(?:search|find)\b", low):
            operations = ["list_directory", "read_metadata"]
        else:
            operations = ["search_files", "list_directory", "read_metadata"]

        extensions: list[str] = []
        if re.search(r"\bpdfs?\b", low):
            extensions.append(".pdf")
        elif re.search(r"\bmarkdown\b|\.md\b", low):
            extensions.append(".md")

        query = ""
        qm = re.search(r"\b(?:for|named|matching|called)\s+([A-Za-z0-9_.\-]+)", raw, re.I)
        if qm and qm.group(1).lower() not in {"all", "my", "the", "this", "files", "documents", "pdf"}:
            query = qm.group(1).strip(" .?!\"'")
        elif re.search(r"\b(.env|id_rsa|id_ed25519|secrets\.json)\b", low):
            query = re.search(r"([A-Za-z0-9_.-]+)", raw).group(1)

        filename = "jarvis_report.md"
        fm = re.search(r"\b([A-Za-z0-9_-]+\.(?:md|txt|json|csv))\b", raw, re.I)
        if fm:
            filename = fm.group(1)
        elif "report" in low:
            filename = "jarvis_report.md"

        return {
            "explicit_path": explicit_path,
            "target": target,
            "is_workspace_only": is_workspace_only,
            "unspecified_folder": unspecified_folder,
            "operations": operations,
            "primary_op": operations[0],
            "extensions": extensions,
            "query": query,
            "filename": filename,
        }

    def _find_matching_project(self, text: str) -> dict | None:
        low = (text or "").lower()
        projects = self.orchestrator.projects.list_projects()
        for p in projects:
            pname = str(p.get("name") or "").strip().lower()
            pbase = Path(str(p.get("root_path") or "")).name.lower()
            if (pname and len(pname) >= 2 and re.search(rf"\b{re.escape(pname)}\b", low)) or (
                pbase and len(pbase) >= 2 and re.search(rf"\b{re.escape(pbase)}\b", low)
            ):
                return p
        return None

    async def _run_erp_dev_review(
        self,
        *,
        project: dict,
        task_id: str | None,
        text: str,
    ) -> dict:
        """Execute a genuine, permission-scoped, read-only project codebase review + safe static verification."""
        import ast

        root = str(project.get("root_path") or "")
        name = str(project.get("name") or Path(root).name)
        plan_steps = [
            f"Verify scoped read permission for project '{name}'",
            "Discover project files & backend modules",
            "Review backend Python/TypeScript structure and syntax via AST",
            "Inspect available safe tests without bypassing execute permission",
            "Summarize findings and update task & knowledge graph",
        ]
        skills_picked = []
        try:
            sel = self.orchestrator.capability_router.select("dev_review", text, max_skills=4)
            skills_picked = [s.get("name") or s.get("skill_id") for s in (sel or []) if isinstance(s, dict)]
        except Exception:
            skills_picked = []
        agents = ["planner", "reviewer", "tester"]
        if task_id:
            await self.orchestrator.tasks.update_task(
                task_id,
                status="running",
                plan=plan_steps,
                selected_agents=agents,
                selected_capabilities=skills_picked,
                current_step="Inspecting project codebase",
            )

        files = await self.orchestrator.fs_access.execute_search_files(
            root, query="", task_id=task_id, limit=80
        )
        backend_files = [f for f in files if f["extension"] == ".py" and "test" not in f["name"].lower()]
        test_files = [f for f in files if "test" in f["name"].lower() and f["extension"] == ".py"]

        ast_issues: list[str] = []
        smells: list[str] = []
        analyzed_funcs = 0
        for bf in backend_files[:8]:
            try:
                doc = await self.orchestrator.fs_access.execute_read_file(bf["path"], task_id=task_id, max_bytes=131_072)
                tree = ast.parse(doc["content"], filename=bf["name"])
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        analyzed_funcs += 1
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                        smells.append(f"{bf['relative_path']}:L{node.lineno} call to {node.func.id}()")
            except SyntaxError as se:
                ast_issues.append(f"{bf['relative_path']}:{se.lineno}: {se.msg}")
            except FilesystemSecurityError:
                continue

        test_cases_found = 0
        for tf in test_files[:6]:
            try:
                tdoc = await self.orchestrator.fs_access.execute_read_file(tf["path"], task_id=task_id, max_bytes=65_536)
                tree = ast.parse(tdoc["content"], filename=tf["name"])
                test_cases_found += sum(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
                    for n in ast.walk(tree)
                )
            except Exception:
                continue

        # Note: Read permission NEVER authorizes shell command execution. Report read-only verification honestly.
        graph_live.filesystem_activity(self, task_id=task_id, folder_path=root, files=files, operation="inspect_project")
        tools_used = ["filesystem_access", "ast_static_review", "test_inventory"]
        listed_names = ", ".join(f["relative_path"] for f in files[:8]) or "none"
        findings_txt = f"Review findings: {'; '.join(smells[:5])}. " if smells else ""
        summary = (
            f"Project `{name}` (`{root}`): found {len(files)} files ({listed_names}) — "
            f"{len(backend_files)} backend modules with {analyzed_funcs} functions/handlers, {len(test_files)} test files with {test_cases_found} test cases. "
            + ("No AST syntax errors found. " if not ast_issues else f"Syntax issues: {'; '.join(ast_issues)}. ")
            + findings_txt
            + "Note: read-only scope inspected files safely without executing shell commands."
        )
        if task_id:
            await self.orchestrator.tasks.update_task(
                task_id,
                status="completed",
                tools_used=tools_used,
                current_step="Completed project review",
                result=summary,
                meta_patch={"project_id": project.get("id"), "files_count": len(files), "test_cases": test_cases_found},
            )
            await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
        return {
            "command": "dev_project_review",
            "reply": summary,
            "paths": [f["path"] for f in files[:12]],
            "files": files,
            "agents": agents,
            "selected_skills": skills_picked,
            "plan": plan_steps,
            "ok": len(ast_issues) == 0,
        }

    async def execute_deferred_action(self, req_data: dict, *, task_id: str | None = None) -> dict:
        """Execute a deferred filesystem/project operation immediately after owner approval."""
        deferred = dict(req_data.get("deferred_action") or {})
        kind = deferred.get("kind") or "fs_search"
        folder = str(deferred.get("folder") or req_data.get("canonical_path") or "")
        query = str(deferred.get("query") or "")
        exts = list(deferred.get("extensions") or [])

        if task_id:
            await self.orchestrator.tasks.update_task(
                task_id,
                status="running",
                current_step=f"Executing approved {kind}",
            )

        try:
            if kind == "erp_dev_review":
                proj = deferred.get("project") or {
                    "id": deferred.get("project_id"),
                    "name": deferred.get("project_name") or Path(folder).name,
                    "root_path": deferred.get("project_path") or folder,
                }
                return await self._run_erp_dev_review(project=proj, task_id=task_id, text=str(deferred.get("original_text") or ""))

            if kind == "fs_write_report":
                filename = str(deferred.get("filename") or "jarvis_report.md")
                target_file = Path(folder) / filename
                report_body = (
                    f"# Jarvis Report\n\n"
                    f"- Request: {deferred.get('original_text', '')}\n"
                    f"- Generated at: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n"
                    f"- Folder: `{folder}`\n"
                )
                out = await self.orchestrator.fs_access.execute_write_file(
                    target_file, report_body, create=True, task_id=task_id
                )
                fitem = {"name": out["name"], "path": out["path"], "size_bytes": out["bytes_written"]}
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=folder, files=[fitem], operation="write_file")
                reply = f"Created report `{out['name']}` at `{out['path']}` ({out['bytes_written']} bytes)."
                if task_id:
                    await self.orchestrator.tasks.update_task(
                        task_id,
                        status="completed",
                        append_tool="filesystem_access",
                        append_file=out["path"],
                        current_step="Created report file",
                        result=reply,
                    )
                    await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
                return {"command": "filesystem_write", "reply": reply, "paths": [out["path"]], "file": out, "ok": True}

            if kind == "fs_read":
                target_path = str(deferred.get("target") or folder)
                doc = await self.orchestrator.fs_access.execute_read_file(target_path, task_id=task_id)
                fitem = {"name": doc["name"], "path": doc["path"], "size_bytes": doc["size_bytes"]}
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=str(Path(doc["path"]).parent), files=[fitem], operation="read_file")
                reply = f"Read `{doc['path']}` ({doc['size_bytes']} bytes): {doc['content'][:300]}"
                if task_id:
                    await self.orchestrator.tasks.update_task(
                        task_id,
                        status="completed",
                        append_tool="filesystem_access",
                        current_step="Completed file read",
                        result=reply,
                    )
                    await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
                return {"command": "filesystem_read", "reply": reply, "paths": [doc["path"]], "document": doc, "ok": True}

            if kind == "fs_delete":
                target_path = str(deferred.get("target") or folder)
                out = await self.orchestrator.fs_access.execute_delete_file(target_path, task_id=task_id)
                reply = f"Deleted file `{out['path']}` after explicit delete approval."
                if task_id:
                    await self.orchestrator.tasks.update_task(
                        task_id,
                        status="completed",
                        append_tool="filesystem_access",
                        current_step="Deleted approved target",
                        result=reply,
                    )
                    await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
                return {"command": "filesystem_delete", "reply": reply, "paths": [], "ok": True}

            # Default: directory search / listing
            if deferred.get("operation") == "list_directory" and not query and not exts:
                entries = await self.orchestrator.fs_access.execute_list_directory(folder, task_id=task_id)
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=folder, files=entries, operation="list_directory")
                names = ", ".join(e["name"] for e in entries[:8]) or "empty folder"
                reply = f"Listed `{folder}` ({len(entries)} items): {names}."
                paths = [e["path"] for e in entries]
                files_out = entries
            else:
                matches = await self.orchestrator.fs_access.execute_search_files(
                    folder, query=query, extensions=exts, task_id=task_id
                )
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=folder, files=matches, operation="search_files")
                if matches:
                    names = ", ".join(m["name"] for m in matches[:8])
                    reply = f"Found {len(matches)} matching file(s) in `{folder}`: {names}."
                else:
                    reply = f"Searched authorized folder `{folder}`, found 0 matching files."
                paths = [m["path"] for m in matches]
                files_out = matches

            if task_id:
                await self.orchestrator.tasks.update_task(
                    task_id,
                    status="completed",
                    append_tool="filesystem_access",
                    current_step="Completed filesystem search",
                    result=reply,
                )
                await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
            return {"command": "filesystem_ops", "reply": reply, "paths": paths, "files": files_out, "ok": True}
        except FilesystemSecurityError as exc:
            if task_id:
                await self.orchestrator.tasks.update_task(
                    task_id,
                    status="failed",
                    current_step="Blocked by filesystem policy",
                    error_info=str(exc),
                )
                await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
            return {"command": "filesystem_blocked", "reply": f"Blocked by filesystem security policy: {exc}", "paths": [], "ok": False}

    async def resume_after_permission_decision(self, request_id: str, decision_result: dict) -> dict:
        """Complete or cancel task flow cleanly after owner decides an inline permission card."""
        req_data = dict(decision_result.get("request") or {})
        task_id = req_data.get("task_id")
        granted = bool(decision_result.get("granted"))
        if not granted:
            reply = f"Access denied. I did not access `{req_data.get('canonical_path') or req_data.get('requested_path')}`."
            task = None
            if task_id:
                task = await self.orchestrator.tasks.cancel_task(task_id, reason="Access denied by owner")
                await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
            self.history.add("assistant", reply)
            self._set_os_state("Completed", source="system", detail="permission_denied")
            self.bus.publish(
                {
                    "type": "assistant_message",
                    "text": reply,
                    "request_id": request_id,
                    "granted": False,
                    "task_id": task_id,
                    "task": task,
                    "paths": [],
                }
            )
            return {"reply": reply, "granted": False, "execution": None, "task": task, "paths": []}

        exec_res = await self.execute_deferred_action(req_data, task_id=task_id)
        reply = str(exec_res.get("reply") or "Completed authorized action.")
        task = await self.orchestrator.tasks.get_task(task_id) if task_id else None
        self.history.add("assistant", reply)
        self._set_os_state("Completed", source="system", detail=exec_res.get("command", "filesystem_ops"))
        self.bus.publish({"type": "graph_changed"})
        self.bus.publish(
            {
                "type": "assistant_message",
                "text": reply,
                "request_id": request_id,
                "granted": True,
                "task_id": task_id,
                "task": task,
                "paths": exec_res.get("paths") or [],
            }
        )
        return {"reply": reply, "granted": True, "execution": exec_res, "task": task, "paths": exec_res.get("paths") or []}

    async def _execute_planned_workflow(
        self,
        text: str,
        plan,
        *,
        source: str,
        cancel_event: asyncio.Event | None,
        t0: float,
        online: bool,
        task_id: str | None = None,
    ) -> dict | None:
        """Execute real backend workflows derived from DecisionEngine routing; return None to stream RAG LLM answer."""
        intent = plan.decision.intent
        workflow = plan.workflow

        if workflow == "clarify" and plan.decision.clarifying_question:
            if task_id:
                await self.orchestrator.tasks.update_task(task_id, status="completed", result=plan.decision.clarifying_question, current_step="Clarification sent")
            return {
                "command": "clarify",
                "reply": plan.decision.clarifying_question,
                "paths": [],
                "ok": True,
            }

        if workflow == "workforce_graph" or plan.decision.department == "dev":
            proj = self._find_matching_project(text)
            if proj is not None:
                proj_root = str(proj.get("root_path") or "")
                # External registered project directories MUST pass scoped FilesystemAccessManager gating
                from .config import ROOT as _JARVIS_ROOT
                try:
                    same_repo = Path(proj_root).resolve() == Path(_JARVIS_ROOT).resolve()
                except Exception:
                    same_repo = False
                if proj_root and not same_repo:
                    chk = await self.orchestrator.fs_access.authorize(proj_root, "list_directory", task_id=task_id)
                    if chk.blocked:
                        if task_id:
                            await self.orchestrator.tasks.update_task(task_id, status="failed", error_info=chk.reason, current_step="Blocked by filesystem policy")
                        return {"command": "filesystem_blocked", "reply": f"Blocked by security policy: {chk.reason}", "paths": [], "ok": False}
                    if not chk.allowed:
                        req_ops = ["list_directory", "search_files", "read_file", "read_metadata"]
                        req = await self.orchestrator.fs_access.create_permission_request(
                            proj_root,
                            req_ops,
                            task_id=task_id,
                            reason=f"Inspect project '{proj.get('name')}' codebase and review backend safely",
                            access_duration="once",
                            deferred_action={
                                "kind": "erp_dev_review",
                                "project": proj,
                                "folder": proj_root,
                                "original_text": text,
                            },
                        )
                        if task_id:
                            await self.orchestrator.tasks.update_task(
                                task_id,
                                status="awaiting_permission",
                                plan=[
                                    f"Request scoped read access to `{proj_root}`",
                                    "Inspect project codebase and backend structure",
                                    "Perform AST/static review & safe test inventory",
                                    "Summarize findings",
                                ],
                                selected_agents=["planner", "reviewer", "tester"],
                                append_permission=req["request_id"],
                                current_step=f"Waiting for permission to read project `{proj.get('name')}`",
                            )
                        self._set_os_state("Waiting for approval", source=source, detail=req["canonical_path"])
                        self.bus.publish({"type": "permission_requested", "request": req, "task_id": task_id})
                        return {
                            "command": "permission_required",
                            "reply": f"Permission required to inspect project `{proj.get('name')}` at `{req['canonical_path']}` ({', '.join(req_ops)}). Approve or deny in the inline permission card.",
                            "paths": [],
                            "permission_request": req,
                            "ok": False,
                        }
                    return await self._run_erp_dev_review(project=proj, task_id=task_id, text=text)

            self._set_os_state("Delegating", source=source, detail=plan.agent_role)
            self._set_os_state("Executing", source=source, detail=intent)
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "workforce_pipeline", query=text[:80])
            res = await asyncio.to_thread(
                self.orchestrator.run_autonomous_dev_pipeline,
                goal=text,
                project_hint=plan.decision.project_hint,
                intent=intent,
                risk_tier=plan.decision.risk_tier,
                filesystem=self.filesystem,
                cancel_check=lambda: bool(cancel_event and cancel_event.is_set()),
            )
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "workforce_pipeline", query=text[:80], ok=bool(res.get("ok")), duration_ms=dur_ms)
            if res.get("status") == "awaiting_approval":
                self._set_os_state("Waiting for approval", source=source, detail=str(res.get("approval_token") or ""))
            drawer = "approvals" if res.get("approval_token") else "workforce"
            reply = str(res.get("summary") or f"Development workflow finished ({res.get('status')}).")
            if res.get("plan") and len(reply) < 350:
                reply = f"{reply}\n\nExecution plan:\n{res['plan']}"
            return {
                "command": "dev_autonomous",
                "reply": reply,
                "paths": [],
                "run_id": res.get("run_id"),
                "approval_token": res.get("approval_token"),
                "plan": res.get("plan"),
                "agents": res.get("agents"),
                "selected_skills": res.get("selected_skills"),
                "ui": {"open_drawer": drawer},
                "ok": bool(res.get("ok")),
            }

        if intent == "document_export":
            self._set_os_state("Delegating", source=source, detail=plan.agent_role)
            self._set_os_state("Executing", source=source, detail="document_export")
            fmt_raw = str((plan.decision.command_payload or {}).get("format") or "pdf").lower()
            fmt_map = {"word": "docx", "powerpoint": "pptx", "excel": "xlsx", "spreadsheet": "xlsx"}
            fmt = fmt_map.get(fmt_raw, fmt_raw if fmt_raw in {"pdf", "docx", "pptx", "xlsx"} else "pdf")
            wf_payload = {"format": fmt, "title": text[:80], "sections": [{"heading": "Summary", "body": text}]}
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "doc_export", query=text[:80])
            res = await asyncio.to_thread(
                self.orchestrator.start_assistant_workflow,
                goal=text,
                task_kind="document_export",
                payload=wf_payload,
            )
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            ok_flag = res.get("state") == "completed"
            graph_live.tool_used(self, "doc_export", query=text[:80], ok=ok_flag, duration_ms=dur_ms)
            art = res.get("artifact") or (res.get("result") or {}).get("artifact")
            paths = [art["path"]] if isinstance(art, dict) and art.get("path") else []
            summary = res.get("summary") or (res.get("result") or {}).get("summary")
            return {
                "command": "document_export",
                "reply": str(summary or f"Workflow {res.get('run_id')} finished with state {res.get('state')}."),
                "paths": paths,
                "run_id": res.get("run_id"),
                "ok": ok_flag,
            }

        if intent == "reminder_manage":
            self._set_os_state("Delegating", source=source, detail=plan.agent_role)
            self._set_os_state("Executing", source=source, detail="reminder")
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "reminders", query=text[:80])
            if re.search(r"\b(?:list|show|what\s+are)\b", text, re.I):
                rems = await asyncio.to_thread(self.orchestrator.reminders.list_reminders)
                dur_ms = (time.perf_counter() - t_start) * 1000.0
                graph_live.tool_used(self, "reminders", query=text[:80], ok=True, duration_ms=dur_ms)
                pending = [r for r in rems if r.get("status") != "completed"]
                if not pending:
                    reply = "You have no pending reminders."
                else:
                    lines = "; ".join(f"{r['title']}" + (f" ({r['due_at']})" if r.get("due_at") else "") for r in pending[:6])
                    reply = f"You have {len(pending)} pending reminder(s): {lines}."
                return {"command": "reminder_list", "reply": reply, "paths": [], "reminders": pending, "ok": True}

            res = await asyncio.to_thread(
                self.orchestrator.start_assistant_workflow,
                goal=text,
                task_kind="reminder",
                payload={"title": text[:120]},
            )
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            ok_flag = res.get("state") == "completed"
            graph_live.tool_used(self, "reminders", query=text[:80], ok=ok_flag, duration_ms=dur_ms)
            summary = res.get("summary") or (res.get("result") or {}).get("summary")
            return {
                "command": "reminder",
                "reply": str(summary or f"Reminder workflow {res.get('run_id')} finished ({res.get('state')})."),
                "paths": [],
                "run_id": res.get("run_id"),
                "ok": ok_flag,
            }

        if intent == "task_status":
            self._set_os_state("Executing", source=source, detail="task_status")
            all_tasks = await self.orchestrator.tasks.list_tasks(limit=25)
            pending_fs = await self.orchestrator.fs_access.list_requests(status="pending")
            pending_ap = await asyncio.to_thread(self.orchestrator.approvals.list_pending)
            active = [
                t
                for t in all_tasks
                if t["status"] in ("queued", "planning", "awaiting_permission", "awaiting_approval", "running", "paused")
                and t.get("task_id") != task_id
            ]
            summary_lines = [f"- [{t['status']}] {t['user_request'][:80]} ({t['current_step']})" for t in active[:6]]
            reply = (
                f"Active/pending assistant tasks: {len(active)}, pending permission requests: {len(pending_fs)}, "
                f"pending action approvals: {len(pending_ap)}."
                + ("\n" + "\n".join(summary_lines) if summary_lines else "")
            )
            if task_id:
                await self.orchestrator.tasks.update_task(task_id, status="completed", append_tool="task_registry", result=reply, current_step="Reported task status")
            return {
                "command": "task_status",
                "reply": reply,
                "paths": [],
                "tasks": all_tasks,
                "pending_permissions": pending_fs,
                "pending_approvals": pending_ap,
                "ui": {"open_drawer": "tasks"},
                "ok": True,
            }

        if intent == "filesystem_ops":
            self._set_os_state("Executing", source=source, detail="filesystem_ops")
            t_start = time.perf_counter()
            fs_info = self._parse_host_fs_request(text)
            if fs_info["is_workspace_only"]:
                graph_live.tool_invoked(self, "filesystem_search", query=text[:80])
                try:
                    wants_inspect, query = self._extract_fs_query(text)
                    if wants_inspect and not query:
                        info = await asyncio.to_thread(self.filesystem.inspect_project)
                        dur_ms = (time.perf_counter() - t_start) * 1000.0
                        graph_live.tool_used(self, "filesystem_search", query=text[:80], ok=True, duration_ms=dur_ms)
                        top = ", ".join(info.get("top_entries", [])[:12]) or "empty"
                        reply = f"Workspace `{info['root']}` contains {info['total_files']} files. Top entries: {top}."
                        if task_id:
                            await self.orchestrator.tasks.update_task(task_id, status="completed", append_tool="filesystem_search", result=reply, current_step="Inspected workspace")
                        return {"command": "filesystem_ops", "reply": reply, "paths": [], "inspection": info, "ok": True}
                    matches = await asyncio.to_thread(self.filesystem.search, query, limit=10)
                    dur_ms = (time.perf_counter() - t_start) * 1000.0
                    graph_live.tool_used(self, "filesystem_search", query=query or text[:80], ok=bool(matches), duration_ms=dur_ms)
                    if not matches:
                        return {"command": "filesystem_ops", "reply": f"No files matched `{query or text}` inside allowlisted directories.", "paths": [], "ok": False}
                    names = ", ".join(m["name"] for m in matches[:6])
                    return {"command": "filesystem_ops", "reply": f"Found {len(matches)} item(s) inside allowlisted roots: {names}.", "paths": [m["path"] for m in matches], "files": matches, "ok": True}
                except FilesystemPermissionError as exc:
                    dur_ms = (time.perf_counter() - t_start) * 1000.0
                    graph_live.tool_used(self, "filesystem_search", query=text[:80], ok=False, duration_ms=dur_ms)
                    return {"command": "filesystem_ops", "reply": str(exc), "paths": [], "ok": False}

            # Scoped Host Filesystem Flow (P10.20)
            target_path = fs_info["target"]
            primary_op = fs_info["primary_op"]
            if fs_info["unspecified_folder"] and primary_op == "search_files":
                active_perms = [
                    p
                    for p in await self.orchestrator.fs_access.list_permissions(status="active")
                    if p["scope"] in ("session", "persistent") and "search_files" in (p.get("granted_operations") or [])
                ]
                if active_perms:
                    target_path = active_perms[0]["canonical_folder_path"]

            if primary_op in WRITE_OPS:
                deferred_kind = "fs_write_report"
            elif primary_op == "read_file":
                deferred_kind = "fs_read"
            elif primary_op == "delete_file":
                deferred_kind = "fs_delete"
            else:
                deferred_kind = "fs_search"

            chk = await self.orchestrator.fs_access.authorize(target_path, primary_op, task_id=task_id)
            if chk.blocked:
                if task_id:
                    await self.orchestrator.tasks.update_task(
                        task_id,
                        status="failed",
                        error_info=chk.reason,
                        current_step="Blocked by filesystem security policy",
                    )
                return {
                    "command": "filesystem_blocked",
                    "reply": f"Blocked by filesystem security policy: {chk.reason}",
                    "paths": [],
                    "ok": False,
                }

            deferred_payload = {
                "kind": deferred_kind,
                "folder": target_path,
                "target": target_path,
                "operation": primary_op,
                "query": fs_info["query"],
                "extensions": fs_info["extensions"],
                "filename": fs_info["filename"],
                "original_text": text,
            }

            if not chk.allowed:
                try:
                    req = await self.orchestrator.fs_access.create_permission_request(
                        target_path,
                        fs_info["operations"],
                        task_id=task_id,
                        reason=f"Requested `{primary_op}` access for: {text[:140]}",
                        access_duration="once",
                        deferred_action=deferred_payload,
                    )
                except FilesystemSecurityError as exc:
                    if task_id:
                        await self.orchestrator.tasks.update_task(task_id, status="failed", error_info=str(exc), current_step="Blocked by filesystem policy")
                    return {"command": "filesystem_blocked", "reply": f"Blocked by filesystem security policy: {exc}", "paths": [], "ok": False}

                if task_id:
                    await self.orchestrator.tasks.update_task(
                        task_id,
                        status="awaiting_permission",
                        plan=[f"Request scoped `{', '.join(fs_info['operations'])}` permission for `{req['canonical_path']}`", f"Execute `{deferred_kind}` inside authorized folder"],
                        append_permission=req["request_id"],
                        current_step=f"Waiting for permission ({', '.join(fs_info['operations'])})",
                    )
                self._set_os_state("Waiting for approval", source=source, detail=req["canonical_path"])
                self.bus.publish({"type": "permission_requested", "request": req, "task_id": task_id})
                return {
                    "command": "permission_required",
                    "reply": f"Permission required before `{primary_op}` on `{req['canonical_path']}`. Choose Allow once, Allow for this session, Always allow (read-only), or Deny.",
                    "paths": [],
                    "permission_request": req,
                    "ok": False,
                }

            return await self.execute_deferred_action(
                {"canonical_path": chk.canonical_path, "deferred_action": deferred_payload},
                task_id=task_id,
            )

        if intent == "memory_manage":
            self._set_os_state("Executing", source=source, detail="memory_manage")
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "memory_store", query=text[:80])
            legacy_items = self.memory_store.list_all(include_pending=False)
            v2_items = await asyncio.to_thread(self.orchestrator.memory.list_personal_memories)
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "memory_store", query=text[:80], ok=True, duration_ms=dur_ms)
            total = len(legacy_items) + len(v2_items)
            preview = [r.text[:60] for r in legacy_items[:3]] + [str(d.get("text") or "")[:60] for d in v2_items[:3]]
            summary_items = "; ".join(p for p in preview if p) or "none stored yet"
            return {
                "command": "memory_manage",
                "reply": f"Stored personal memories: {total} ({len(legacy_items)} local store, {len(v2_items)} structured). Recent: {summary_items}.",
                "paths": [],
                "ui": {"open_drawer": "memory"},
                "ok": True,
            }

        if intent == "system_status":
            self._set_os_state("Executing", source=source, detail="system_status")
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "system_monitor", query="status")
            st = await self.status()
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "system_monitor", query="status", ok=True, duration_ms=dur_ms)
            ram = st["system"]["ram"]
            gpu = st["system"]["gpu"]
            cpu = st["system"]["cpu_pct"]
            gpu_txt = f"GPU {gpu['name']} ({gpu['used_gb']}/{gpu['total_gb']} GB VRAM, {gpu['util_pct']}% util)" if gpu else "GPU not reported"
            cpu_txt = f"CPU {cpu}%" if cpu is not None else "CPU sampling"
            reply = (
                f"System health: {cpu_txt}, RAM {ram['free_gb']}/{ram['total_gb']} GB free, {gpu_txt}. "
                f"Active model: {st['brain']['model']} (Ollama {'up' if st['ollama'].get('up') else 'down'}). "
                f"Voice: {st['voice']['audio_state']} ({st['voice']['mode']}), pending approvals: {st['tasks']['pending_approvals']}."
            )
            return {"command": "system_status", "reply": reply, "paths": [], "ui": {"open_drawer": "system"}, "ok": True}

        if intent == "docker_ops":
            import subprocess
            self._set_os_state("Executing", source=source, detail="docker_ops")
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "docker_inspect", query="docker_status")
            available = await asyncio.to_thread(git_worktree.is_docker_available)
            containers = []
            if available:
                try:
                    cp = await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "ps", "--format", "{{.Names}} ({{.Status}}, {{.Image}})"],
                        capture_output=True,
                        text=True,
                        timeout=4,
                        shell=False,
                    )
                    if cp.returncode == 0:
                        containers = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
                except Exception:
                    containers = []
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "docker_inspect", query="docker_status", ok=available, duration_ms=dur_ms)
            img = self.settings.workforce.docker_image
            backend = self.settings.workforce.sandbox_backend
            if not available:
                reply = (
                    f"Docker daemon is not running on this host (sandbox_backend={backend}, configured image=`{img}`). "
                    "Start Docker Desktop to enable isolated container execution."
                )
            else:
                clist = ", ".join(containers[:6]) if containers else "none running"
                reply = f"Docker daemon is online (configured sandbox image=`{img}`). Active containers: {clist}."
            return {"command": "docker_ops", "reply": reply, "paths": [], "docker_available": available, "containers": containers, "ok": available}

        if intent == "recovery_ops":
            self._set_os_state("Executing", source=source, detail="recovery_ops")
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "recovery_status", query="backups")
            manifests = await asyncio.to_thread(self.orchestrator.recovery.list_manifests)
            targets = await asyncio.to_thread(self.orchestrator.recovery.list_database_targets)
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "recovery_status", query="backups", ok=True, duration_ms=dur_ms)
            latest = manifests[0] if manifests else None
            latest_txt = f"Latest backup `{latest.get('backup_id')}` ({latest.get('tier')})." if latest else "No backups recorded yet."
            reply = (
                f"Recovery status: {len(manifests)} backup manifest(s), {len(targets)} database target(s). "
                f"{latest_txt} Production restores remain strictly gated by owner approval."
            )
            return {"command": "recovery_ops", "reply": reply, "paths": [], "manifests": manifests, "targets": targets, "ok": True}

        if intent == "web_research" and not online:
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "knowledge_search", query=text[:80])
            khits = self.knowledge.search(text, k=3)
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "knowledge_search", query=text[:80], ok=True, duration_ms=dur_ms)
            if khits:
                snippets = "; ".join(f"[{h.get('title') or h.get('source_id') or 'kb'}] {str(h.get('content') or '')[:160]}" for h in khits)
                reply = (
                    "Jarvis is in Local Mode (live internet search is disabled), "
                    f"so I checked your offline knowledge index instead: {snippets}"
                )
            else:
                reply = "Jarvis is in Local Mode (internet access is disabled). Enable Online Assisted Mode to run live web research."
            return {"command": "web_offline", "reply": reply, "paths": [], "ok": False}

        return None

    def _speak_sentences(self, buf: str, flush: bool = False) -> str:
        m = None
        for m in SENT_END.finditer(buf):
            pass
        if m and len(buf[:m.end()]) >= 100:
            self.speaker.say(buf[:m.end()])
            buf = buf[m.end():]
        if flush and buf.strip():
            self.speaker.say(buf)
            return ""
        return buf

    async def stream_turn(self, text: str, speak: bool = False, source: str = "text", cancel_event: asyncio.Event | None = None) -> AsyncIterator[dict]:
        """Yields {"type": "meta"|"token"|"tool"|"reset_answer"|"interrupted"|"done"|"error", ...}. The only place a turn is executed."""
        text = text.strip()
        eff_cancel = cancel_event or asyncio.Event()
        async with self._turn_lock:
            self._active_turn_cancel = eff_cancel
            self.speaker.stop()  # new input interrupts any ongoing speech
            self._set_activity("thinking")
            self._set_os_state("Understanding", source=source)
            t0 = time.time()
            current_task_id: str | None = None
            try:
                if eff_cancel.is_set():
                    yield {"type": "interrupted"}
                    return
                ui_cmd = self._match_ui_command(text)
                if ui_cmd:
                    self._set_os_state("Executing", source=source, detail=ui_cmd["command"])
                    reply = ui_cmd["reply"]
                    yield {"type": "meta", "command": ui_cmd["command"], "nodes": [], "model": None, "web_sources": []}
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak:
                        self.speaker.say(reply)
                    self._set_os_state("Completed", source=source, detail=ui_cmd["command"])
                    self.bus.publish({"type": "ui_action", "ui": ui_cmd["ui"]})
                    yield {
                        "type": "done",
                        "answer": reply,
                        "nodes": [],
                        "paths": [],
                        "web_sources": [],
                        "ui": ui_cmd["ui"],
                        "command": ui_cmd["command"],
                        "ok": True,
                        "seconds": round(time.time() - t0, 2),
                    }
                    return

                cmd = await commands.route(self, text)
                if cmd:
                    self._set_os_state("Executing", source=source, detail=cmd["name"])
                    if cmd["name"] in {
                        "search_files", "read_file", "open_file", "open_app", "inspect_project",
                        "knowledge_sync", "learning_status", "prepare_dataset", "training_request", "evaluate_model",
                    }:
                        graph_live.tool_used(self, cmd["name"], query=text[:80], ok=cmd.get("ok", True))
                    reply = cmd["reply"]
                    yield {"type": "meta", "command": cmd["name"], "nodes": cmd["nodes"], "model": None, "web_sources": []}
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak and cmd.get("speak", True):
                        self.speaker.say(reply)
                    self._set_os_state("Completed", source=source, detail=cmd["name"])
                    if cmd.get("ui"):
                        self.bus.publish({"type": "ui_action", "ui": cmd["ui"]})
                    yield {
                        "type": "done",
                        "answer": reply,
                        "nodes": cmd["nodes"],
                        "paths": cmd.get("paths") or ([cmd["note"]] if cmd.get("note") else []),
                        "web_sources": [],
                        "ui": cmd.get("ui"),
                        "command": cmd["name"],
                        "ok": cmd.get("ok", True),
                        "seconds": round(time.time() - t0, 2),
                    }
                    return

                self._set_os_state("Planning", source=source)
                plan = await self.orchestrator.decision.decide(text)
                task_row = await self.orchestrator.tasks.create_task(
                    text,
                    source=source,
                    intent=plan.decision.intent,
                    status="planning",
                    selected_agents=[plan.agent_role] if plan.agent_role else [],
                    current_step=f"Planning {plan.workflow}",
                )
                current_task_id = task_row["task_id"]
                decision_summary: dict[str, object] = {
                    "department": plan.decision.department,
                    "intent": plan.decision.intent,
                    "complexity": plan.decision.complexity,
                    "workflow": plan.workflow,
                    "agent_role": plan.agent_role,
                    "requires_owner_approval": plan.decision.requires_owner_approval,
                }
                graph_live.publish_os_event(
                    self.bus,
                    "decision.routed",
                    legacy_type="decision",
                    source_department=plan.decision.department,
                    payload=decision_summary,
                    department=plan.decision.department,
                    intent=plan.decision.intent,
                    workflow=plan.workflow,
                )

                # Enforce owner approval gate for explicitly privileged/destructive intents before any execution.
                if plan.decision.requires_owner_approval:
                    flags = ", ".join(plan.decision.sensitive_flags) or "privileged operation"
                    if "file_deletion" in (plan.decision.sensitive_flags or []):
                        fs_del = self._parse_host_fs_request(text)
                        if not fs_del["is_workspace_only"]:
                            chk_del = await self.orchestrator.fs_access.authorize(fs_del["target"], "delete_file", task_id=current_task_id)
                            if chk_del.blocked:
                                await self.orchestrator.tasks.update_task(
                                    current_task_id,
                                    status="failed",
                                    error_info=chk_del.reason,
                                    current_step="Blocked by filesystem security policy",
                                )
                                reply = f"Blocked by filesystem security policy: {chk_del.reason}"
                                yield {"type": "meta", "command": "filesystem_blocked", "nodes": [], "model": None, "web_sources": [], "decision": decision_summary, "task_id": current_task_id}
                                yield {"type": "token", "text": reply}
                                self.history.add("user", text)
                                self.history.add("assistant", reply)
                                yield {"type": "done", "answer": reply, "nodes": [], "paths": [], "web_sources": [], "command": "filesystem_blocked", "task_id": current_task_id, "ok": False, "seconds": round(time.time() - t0, 2)}
                                return

                    req = await asyncio.to_thread(
                        self.orchestrator.approvals.create_request,
                        action_type="privileged_command",
                        target_resource=text[:140],
                        preview_diff=f"Blocked by Layer 3 policy gate ({flags}):\n{text[:400]}",
                        args={"source": source, "sensitive_flags": list(plan.decision.sensitive_flags), "task_id": current_task_id},
                    )
                    updated_task = await self.orchestrator.tasks.update_task(
                        current_task_id,
                        status="awaiting_approval",
                        append_permission=req["token"],
                        current_step=f"Waiting for owner approval ({flags})",
                    )
                    self._set_os_state("Waiting for approval", source=source, detail=flags, approval_token=req["token"])
                    self.bus.publish({"type": "approval_requested", "approval": req, "task": updated_task, "task_id": current_task_id})
                    graph_live.emit(
                        self.bus,
                        "agent.progress",
                        source_department=plan.decision.department,
                        state="awaiting_approval",
                        highlight=[f"agent:{plan.agent_role}"],
                        resync=True,
                    )
                    reply = f"Owner approval required before executing ({flags}). Review and approve request `{req['token']}` inline or in Approvals."
                    yield {"type": "meta", "command": "approval_required", "nodes": [], "model": None, "web_sources": [], "decision": decision_summary, "approval": req, "task": updated_task, "task_id": current_task_id}
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak:
                        self.speaker.say(reply)
                    yield {
                        "type": "done",
                        "answer": reply,
                        "nodes": [],
                        "paths": [],
                        "web_sources": [],
                        "command": "approval_required",
                        "approval_token": req["token"],
                        "approval": req,
                        "task": updated_task,
                        "task_id": current_task_id,
                        "ok": False,
                        "seconds": round(time.time() - t0, 2),
                    }
                    return

                online = bool(self.settings.internet.enabled and audit.is_online_enabled())
                planned = await self._execute_planned_workflow(
                    text,
                    plan,
                    source=source,
                    cancel_event=eff_cancel,
                    t0=t0,
                    online=online,
                    task_id=current_task_id,
                )
                if planned is not None:
                    if planned.get("interrupted") or eff_cancel.is_set():
                        await self.orchestrator.tasks.cancel_task(current_task_id, reason="Interrupted by user")
                        self.speaker.stop()
                        yield {"type": "interrupted"}
                        return
                    reply = planned["reply"]
                    cmd_name = planned["command"]
                    if cmd_name not in ("permission_required", "approval_required") and not (
                        cmd_name == "dev_autonomous" and planned.get("approval_token")
                    ):
                        await self.orchestrator.tasks.update_task(
                            current_task_id,
                            status="completed" if planned.get("ok", True) else ("failed" if cmd_name == "filesystem_blocked" else "completed"),
                            result=reply,
                            generated_files=planned.get("paths") or [],
                            current_step=f"Finished {cmd_name}",
                        )
                        await self.orchestrator.fs_access.consume_task_once_permissions(current_task_id)
                    elif planned.get("approval_token"):
                        await self.orchestrator.tasks.update_task(
                            current_task_id,
                            status="awaiting_approval",
                            append_permission=planned.get("approval_token"),
                            current_step="Waiting for owner approval",
                        )
                    latest_task = await self.orchestrator.tasks.get_task(current_task_id)
                    yield {
                        "type": "meta",
                        "command": planned["command"],
                        "nodes": [],
                        "model": None,
                        "web_sources": [],
                        "decision": decision_summary,
                        "permission_request": planned.get("permission_request"),
                        "task": latest_task,
                        "task_id": current_task_id,
                    }
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak and reply:
                        self.speaker.say(reply)
                    if self.os_state != "Waiting for approval":
                        self._set_os_state("Completed" if planned.get("ok", True) else "Idle", source=source, detail=planned["command"])
                    if planned.get("ui"):
                        self.bus.publish({"type": "ui_action", "ui": planned["ui"]})
                    yield {
                        "type": "done",
                        "answer": reply,
                        "nodes": [],
                        "paths": planned.get("paths") or [],
                        "web_sources": [],
                        "command": planned["command"],
                        "run_id": planned.get("run_id"),
                        "approval_token": planned.get("approval_token"),
                        "permission_request": planned.get("permission_request"),
                        "task": latest_task,
                        "task_id": current_task_id,
                        "plan": planned.get("plan"),
                        "agents": planned.get("agents"),
                        "selected_skills": planned.get("selected_skills"),
                        "ui": planned.get("ui"),
                        "ok": bool(planned.get("ok", True)),
                        "seconds": round(time.time() - t0, 2),
                    }
                    return

                self._set_os_state("Thinking", source=source, detail=plan.decision.intent)
                ctx_bundle = self._gather_context(text)
                hits = ctx_bundle["hits"]
                graph_live.conversation_used(self, hits, ctx_bundle)
                nodes = ctx_bundle["nodes"]
                notes_ctx = ctx_bundle["notes_ctx"]
                today = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
                web_sources: list[dict] = []

                yield {
                    "type": "meta",
                    "nodes": nodes,
                    "model": self.brain.spec.model,
                    "provider": getattr(self.brain, "provider", "ollama"),
                    "sources": [n.title for n, _ in hits],
                    "memory_ids": ctx_bundle["memory_ids"],
                    "knowledge_sources": ctx_bundle["knowledge_sources"],
                    "decision": decision_summary,
                    "online": online,
                    "web_sources": [],
                }

                # Local Mode (default): incremental sentence-level streaming LLM -> Piper TTS
                if not online:
                    msgs = build_messages(
                        self.history.window(),
                        text,
                        notes_ctx,
                        today,
                        online=False,
                        memories=ctx_bundle["memories_ctx"] or None,
                        history_hits=ctx_bundle["history_ctx"] or None,
                        feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                        knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                    )
                    out_buf: list[str] = []
                    async for ev in self._stream_llm_with_tts(msgs, speak=speak, cancel_event=eff_cancel, answer_out=out_buf):
                        yield ev
                        if ev["type"] == "interrupted":
                            return
                    answer = out_buf[-1] if out_buf else ""
                else:
                    # Online Assisted Mode: try Ollama tool calling first (if enabled), with deterministic fallback
                    msgs = build_messages(
                        self.history.window(),
                        text,
                        notes_ctx,
                        today,
                        online=True,
                        memories=ctx_bundle["memories_ctx"] or None,
                        history_hits=ctx_bundle["history_ctx"] or None,
                        feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                        knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                    )
                    tools = _web_tools(self.settings.internet.allow_page_fetch) if self.settings.internet.tool_calling else None
                    answer, chunker = "", StreamingSpeechBuffer()
                    tool_calls: list[dict] = []
                    tools_supported = tools is not None

                    if tools_supported:
                        try:
                            async for ev in self.brain.stream_events(msgs, tools=tools):
                                if eff_cancel.is_set():
                                    self.speaker.stop()
                                    yield {"type": "interrupted"}
                                    return
                                if ev["type"] == "token":
                                    answer += ev["text"]
                                    yield {"type": "token", "text": ev["text"]}
                                elif ev["type"] == "tool_calls":
                                    tool_calls = ev["calls"]
                        except ToolsUnsupportedError:
                            tools_supported = False
                            answer = ""

                    if tool_calls:
                        if answer:
                            yield {"type": "reset_answer"}
                        answer = ""
                        chunker.reset()
                        assistant_calls = [
                            {"type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                            for c in tool_calls
                        ]
                        msgs = [*msgs, {"role": "assistant", "content": "", "tool_calls": assistant_calls}]
                        for c in tool_calls[:3]:
                            if eff_cancel.is_set():
                                self.speaker.stop()
                                yield {"type": "interrupted"}
                                return
                            yield {"type": "tool", "status": "searching", "name": c["name"], "query": c["arguments"].get("query") or c["arguments"].get("url") or ""}
                            self._set_os_state("Executing", source=source, detail=c["name"])
                            t_tool = time.perf_counter()
                            q_str = str(c["arguments"].get("query") or c["arguments"].get("url") or "")
                            graph_live.tool_invoked(self, c["name"], query=q_str)
                            tool_out, srcs = await self._execute_tool(c)
                            graph_live.tool_used(self, c["name"], query=q_str, ok=("error" not in tool_out[:40]), duration_ms=(time.perf_counter() - t_tool) * 1000.0)
                            self._set_os_state("Thinking", source=source)
                            web_sources.extend(srcs)
                            msgs.append({"role": "tool", "content": tool_out})
                        out_buf = []
                        async for ev in self._stream_llm_with_tts(msgs, speak=speak, cancel_event=cancel_event, answer_out=out_buf):
                            yield ev
                            if ev["type"] == "interrupted":
                                return
                        answer = out_buf[-1] if out_buf else ""
                    elif (not tools_supported or not answer) or needs_web_intent(text, has_notes=bool(hits)):
                        if not tools_supported or needs_web_intent(text, has_notes=bool(hits)):
                            q = extract_web_query(text)
                            if answer:
                                yield {"type": "reset_answer"}
                            answer = ""
                            chunker.reset()
                            yield {"type": "tool", "status": "searching", "name": "web_search", "query": q}
                            self._set_os_state("Executing", source=source, detail="web_search")
                            t_tool = time.perf_counter()
                            graph_live.tool_invoked(self, "web_search", query=q)
                            try:
                                res = await self.search.search(q)
                                graph_live.tool_used(self, "web_search", query=q, ok=True, duration_ms=(time.perf_counter() - t_tool) * 1000.0)
                                self._set_os_state("Thinking", source=source)
                                web_sources = res["results"]
                                msgs = build_messages(
                                    self.history.window(),
                                    text,
                                    notes_ctx,
                                    today,
                                    online=True,
                                    web_results=web_sources,
                                    memories=ctx_bundle["memories_ctx"] or None,
                                    history_hits=ctx_bundle["history_ctx"] or None,
                                    feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                                    knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                                )
                            except SearchError as e:
                                msgs = build_messages(
                                    self.history.window(),
                                    text,
                                    notes_ctx,
                                    today,
                                    online=True,
                                    web_results=[{"title": "Web search error", "url": "", "snippet": str(e)}],
                                    memories=ctx_bundle["memories_ctx"] or None,
                                    history_hits=ctx_bundle["history_ctx"] or None,
                                    feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                                    knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                                )
                            out_buf = []
                            async for ev in self._stream_llm_with_tts(msgs, speak=speak, cancel_event=eff_cancel, answer_out=out_buf):
                                yield ev
                                if ev["type"] == "interrupted":
                                    return
                            answer = out_buf[-1] if out_buf else ""
                        else:
                            if speak and answer.strip():
                                self.speaker.say(answer)
                    else:
                        if speak and answer.strip():
                            self.speaker.say(answer)

                answer = answer.strip()
                if not answer:
                    raise BrainError("The model returned an empty reply.")
                self.history.add("user", text)
                self.history.add("assistant", answer)
                st = self.brain.last_stats
                # Deduplicate source URLs preserving order
                seen_urls = set()
                uniq_sources = []
                for s in web_sources:
                    u = s.get("url")
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        uniq_sources.append(s)
                if current_task_id:
                    await self.orchestrator.tasks.update_task(
                        current_task_id,
                        status="completed",
                        result=answer[:1000],
                        current_step="Responded via LLM",
                    )
                    await self.orchestrator.fs_access.consume_task_once_permissions(current_task_id)
                latest_task = await self.orchestrator.tasks.get_task(current_task_id) if current_task_id else None
                yield {
                    "type": "done",
                    "answer": answer,
                    "nodes": nodes,
                    "paths": [n.path for n, _ in hits],
                    "memory_ids": ctx_bundle["memory_ids"],
                    "knowledge_sources": ctx_bundle["knowledge_sources"],
                    "web_sources": uniq_sources,
                    "command": None,
                    "task": latest_task,
                    "task_id": current_task_id,
                    "ok": True,
                    "seconds": round(time.time() - t0, 2),
                    "ttft": round(st.get("ttft") or 0, 2),
                    "tps": round(st.get("tps", 0), 1),
                    "model": st.get("model"),
                    "truncated": st.get("truncated", False),
                }
            except BrainError as e:
                if current_task_id:
                    await self.orchestrator.tasks.update_task(current_task_id, status="failed", error_info=str(e), current_step="Model error")
                self._set_os_state("Error", source=source, detail=str(e))
                if speak:
                    self.speaker.say("I'm afraid I can't reach my language model, sir.")
                yield {"type": "error", "message": str(e)}
            finally:
                if self._active_turn_cancel is eff_cancel:
                    self._active_turn_cancel = None
                next_act = "speaking" if self.speaker.speaking else "idle"
                self._set_activity(next_act)
                if self.os_state not in ("Error", "Waiting for approval"):
                    rest = "Listening" if self.voice.mic_active else "Idle"
                    self._set_os_state("Speaking" if self.speaker.speaking else rest, source=source)

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

    def _set_os_state(self, state: str, *, source: str = "text", detail: str = "", **extra):
        if state == "Listening" and source != "browser_mic" and not self.voice.mic_active:
            state = "Idle"
        if state != self.os_state or detail:
            self.os_state = state
            graph_live.emit_assistant_state(self.bus, state, source=source, detail=detail, **extra)

    # ---- status --------------------------------------------------------------------------------------
    async def status(self) -> dict:
        h = await self.brain.health()
        return {
            "ollama": h,
            "brain": {
                "key": self.brain.current,
                "model": self.brain.spec.model,
                "provider": getattr(self.brain, "provider", "ollama"),
                "thinking": self.brain.spec.thinking,
                "num_ctx": self.brain.spec.num_ctx,
            },
            "voice": {
                "state": self.voice.state,
                "audio_state": self.voice.audio_state.value,
                "mode": self.voice.mode,
                "paused": self.voice.paused,
                "mic_suspended": self.voice.mic_suspended,
                "mic_active": self.voice.mic_active,
                "error": self.voice.last_error,
                "wake_enabled": self.settings.wake.enabled,
                "wake_ready": self.wake.ready,
                "speaking": self.speaker.speaking,
                "barge_in_enabled": self.settings.audio.barge_in_enabled,
                "suppress_mic_during_tts": self.settings.audio.suppress_mic_during_tts,
                "duplex": self.voice.duplex_info(),
                "tts_status": self.speaker.status(),
            },
            "stt": {
                "model": self.settings.stt.model,
                "language": self.settings.stt.language,
                "quality": self.settings.stt.quality,
            },
            "translation": self.settings.translation.model_dump(),
            "ready": {
                "tts": self.tts.ready,
                "stt": self.stt.ready,
                "wake": self.wake.ready,
                **{f"{k}_warm": v for k, v in self.warm_status.items()},
            },
            "notes": len(self.store.all()),
            "memory": {"count": len(self.memory_store.list_all(include_pending=False)), "pending": len([r for r in self.memory_store.list_all(include_pending=True) if not r.confirmed])},
            "tasks": {"recent": self.actions.tasks.list_recent(limit=5), "pending_approvals": len(self.actions.pending_approvals())},
            "learning": {
                "enabled": self.settings.learning.enabled,
                "kb_chunks": self.knowledge.freshness().get("kb_chunks", 0),
                "feedback": self.feedback.summary().get("by_status"),
                "active_version_id": self.registry.summary().get("active_version_id"),
            },
            "activity": self.activity,
            "tts": {"enabled": self.settings.tts.enabled, "voice": self.settings.tts.voice, "speed": self.settings.tts.speed, "voices": len(self.tts.list_voices())},
            "offline_enforced": self.settings.offline_enforced,
            "mode": self.settings.mode,
            "internet": self.search.status(),
            "system": {
                "ram": sysinfo.ram(),
                "gpu": await asyncio.to_thread(sysinfo.gpu),
                "cpu_pct": sysinfo.cpu(),
                "audio": await asyncio.to_thread(sysinfo.audio),
            },
        }
