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
from .proactive import ProactiveSuggestionEngine
from .actions import ActionExecutor
from .aec import EchoReferenceBuffer, NLMSEchoCanceller
from .agents.multistep import build_multistep_plan, classify_task_category
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
from .translator import Translator, transcript_requires_voice_clarification
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


def _fs_tools() -> list[dict]:
    """Read-only, permission-gated filesystem & document inspection tool schemas for Brain."""
    return [
        {
            "type": "function",
            "function": {
                "name": "fs_get_project_structure",
                "description": "Inspect the directory tree, top folders/files, and stack markers of an approved folder or project alias.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Folder path or approved project alias (e.g. 'ERP_V2' or 'Documents')"},
                        "max_depth": {"type": "integer", "description": "Directory traversal depth (1-5, default 3)"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fs_list_directory",
                "description": "List immediate files and subdirectories inside an approved directory or project alias.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Directory path or project alias"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fs_search_files",
                "description": "Search filenames recursively inside an approved folder or project alias.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Filename substring or keyword (e.g. 'database', 'config', 'settings')"},
                        "path": {"type": "string", "description": "Optional root folder path or alias"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fs_find_files_by_extension",
                "description": "Find files with specific extensions (e.g. ['.py', '.json']) inside an approved folder or alias.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "extensions": {"type": "array", "items": {"type": "string"}, "description": "File extensions such as ['.py', '.yaml']"},
                        "path": {"type": "string", "description": "Optional root folder path or alias"},
                    },
                    "required": ["extensions"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fs_read_file",
                "description": "Read text/code/config file contents safely inside an approved folder (secrets auto-redacted). Can also read PDF/DOCX/XLSX.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Full path or alias-relative path (e.g. 'ERP_V2/backend/config.py')"},
                        "start_line": {"type": "integer", "description": "Optional 1-based start line"},
                        "max_lines": {"type": "integer", "description": "Optional max lines to return (default 200)"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fs_read_document",
                "description": "Extract structured readable text and page/sheet/slide metadata from PDF, DOCX, PPTX, XLSX, CSV, or Markdown documents.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Full or alias-relative path to the document"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fs_read_metadata",
                "description": "Inspect file or directory metadata (size, timestamps, type) without reading contents.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "File or folder path or alias"}},
                    "required": ["path"],
                },
            },
        },
    ]


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


def _assistant_tools(*, include_fs: bool, include_web: bool, allow_page_fetch: bool) -> list[dict]:
    tools: list[dict] = []
    if include_fs:
        tools.extend(_fs_tools())
    if include_web:
        tools.extend(_web_tools(allow_page_fetch))
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
        self.proactive = ProactiveSuggestionEngine(settings.proactive)
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
        try:
            self.translator.records.attach_db(self.orchestrator.db.sync_session)
        except Exception:
            pass
        self.orchestrator.attach_research_deps(
            search_service=self.search,
            knowledge_pipeline=self.knowledge,
            online_allowed_fn=online_fn,
        )

    # ---- lifecycle -----------------------------------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.bus.loop = self.loop
        try:
            recovery = await self.orchestrator.task_runner.start()
            if any(recovery.values()):
                self.bus.publish({"type": "task_recovery_boot", "report": recovery})
        except Exception as e:
            log.warning("Background task runner startup recovery skipped: %s", e)
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
        try:
            await self.orchestrator.task_runner.stop()
        except Exception as exc:
            log.debug("Background task runner shutdown error ignored cleanly: %s", exc)
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
        if hasattr(self.stt, "configure"):
            self.stt.configure(new.stt)
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
        if getattr(self, "orchestrator", None) is not None and getattr(self.orchestrator, "memory", None) is not None:
            self.orchestrator.memory.mem_spec = new.memory
        self.settings.filesystem = new.filesystem
        self.filesystem.spec = new.filesystem
        self.settings.actions = new.actions
        self.actions.spec = new.actions
        self.settings.monitoring = new.monitoring
        self.settings.proactive = new.proactive
        self.proactive.update_spec(new.proactive)
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
        meta = self.voice.pop_utterance_meta(utt_id) if hasattr(self.voice, "pop_utterance_meta") else {}
        try:
            if meta.get("raw_audio") is not None:
                stt_res = await asyncio.to_thread(
                    self.stt.transcribe,
                    meta["raw_audio"],
                    sample_rate=int(meta.get("sample_rate", 16000) or 16000),
                    channels=int(meta.get("channels", 1) or 1),
                )
            else:
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
            payload={"text": text, "language": stt_res.get("detected_language", stt_res.get("language", "unknown"))},
            text=text,
        )

        rec = await self.translator.process_stt(stt_res, self.settings.translation, audio=audio_f32)
        self.bus.publish({"type": "transcript_record", **rec.to_dict()})
        use_translated = bool(rec.translated_text and rec.target_language == "en" and rec.detected_language != "en")
        prompt_text = ((rec.translated_text if use_translated else rec.corrected_transcript) or "").strip()
        if not prompt_text or self._cancel_event.is_set():
            return self._cancel_event.is_set()

        if clarify_msg := self._voice_clarification_message(rec, prompt_text):
            self.bus.publish({
                "type": "clarification_required",
                "message": clarify_msg,
                "transcript_id": rec.id,
                "heard": prompt_text,
            })
            self.history.add("user", prompt_text)
            self.history.add("assistant", clarify_msg)
            if self.settings.tts.enabled:
                self.speaker.say(clarify_msg)
            return True

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
        if clarify_msg := self._voice_clarification_message(rec, prompt_text):
            self.bus.publish({
                "type": "clarification_required",
                "message": clarify_msg,
                "transcript_id": rec.id,
                "heard": prompt_text,
            })
            return
        if not prompt_text:
            return
        self.bus.publish({"type": "user", "text": prompt_text, "transcript_id": rec.id})
        async for ev in self.stream_turn(prompt_text, speak=True, source="voice"):
            self.bus.publish(ev)

    def _voice_clarification_message(self, rec, prompt_text: str) -> str | None:
        return transcript_requires_voice_clarification(rec, prompt_text)

    def _build_messages(self, *args, **kwargs):
        kwargs.setdefault("personality_preset", self.settings.preferences.personality_preset)
        kwargs.setdefault("custom_instructions", self.settings.preferences.custom_instructions)
        return build_messages(*args, **kwargs)

    def _gather_context(self, text: str) -> dict:
        """Collect provenance-separated personal memory, history, feedback, notes, and indexed knowledge."""
        q = text
        if len(text.split()) < 4 and self.history.items:  # short follow-up: borrow previous question keywords
            prev = next((m["content"] for m in reversed(self.history.items) if m["role"] == "user"), "")
            q = f"{prev} {text}"
        hits = [(n, s) for n, s in search(self.store.all(), q, self.settings.context_notes) if s >= MIN_SCORE]

        memories_ctx: list[tuple[str, str]] = []
        memory_ids: list[str] = []
        seen_mem_texts: set[str] = set()
        five_way = getattr(getattr(self, "orchestrator", None), "memory", None)
        mem_enabled = bool(getattr(self.settings.memory, "enabled", True))
        if mem_enabled:
            mem_hits = [(r, s) for r, s in self.memory_store.search(q, limit=4) if s >= 1.5]
            for r, _ in mem_hits:
                if five_way is not None and not five_way.is_category_enabled(r.category):
                    continue
                norm_t = r.text.strip().lower()
                if norm_t and norm_t not in seen_mem_texts:
                    seen_mem_texts.add(norm_t)
                    memories_ctx.append((r.category, r.text))
                    memory_ids.append(r.id)
            if five_way is not None:
                try:
                    five_hits = five_way.search_personal_memory(q, k=4)
                    ret_ids: list[str] = []
                    for fh in five_hits:
                        if float(fh.get("score") or 0.0) < 0.25:
                            continue
                        ct = str(fh.get("content") or "").strip()
                        norm_t = ct.lower()
                        if ct and norm_t not in seen_mem_texts:
                            seen_mem_texts.add(norm_t)
                            memories_ctx.append((str(fh.get("memory_type") or "explicit"), ct))
                            memory_ids.append(str(fh["id"]))
                            ret_ids.append(str(fh["id"]))
                    if ret_ids:
                        five_way.mark_retrieved(ret_ids)
                except Exception:
                    pass

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
            "memories_ctx": memories_ctx[:6],
            "memory_ids": memory_ids[:6],
            "history_ctx": history_ctx,
            "feedback_ctx": feedback_ctx,
            "knowledge_ctx": knowledge_ctx,
            "knowledge_sources": [{"source_id": k["source_id"], "title": k["title"], "trust": k["trust"]} for k in kb_raw],
        }

    def _record_adaptive_turn_memory(
        self,
        text: str,
        *,
        cmd_name: str = "",
        intent: str = "",
        paths: list[str] | None = None,
        project_alias: str | None = None,
        ok: bool = True,
    ) -> None:
        """Persist safe preference/project/command/conversation context without storing secrets or failed calls."""
        if not ok or not bool(getattr(self.settings.memory, "enabled", True)):
            return
        five_way = getattr(getattr(self, "orchestrator", None), "memory", None)
        if five_way is None:
            return
        from .security.pii_firewall import detect_secret

        cleaned = (text or "").strip()
        if not cleaned or detect_secret(cleaned):
            return
        try:
            if project_alias and (paths or []):
                names = ", ".join(Path(p).name for p in (paths or [])[:4])
                five_way.record_adaptive_memory(
                    content=f"Project {project_alias} inspection touched: {names}",
                    category="project_memory",
                    topic=str(project_alias),
                    title=f"Project {project_alias}",
                    source="command_derived",
                )
            if cmd_name and cmd_name not in ("task_status", "repeat_last_command", "show_recent_tasks", "show_failed_tasks", "last_request_status", "continue_task", "cancel_task"):
                five_way.record_adaptive_memory(
                    content=f"Executed workflow '{intent or cmd_name}': {cleaned[:160]}",
                    category="command_memory",
                    topic=intent or cmd_name,
                    title=f"Command: {intent or cmd_name}",
                    source="command_derived",
                )
            elif len(cleaned.split()) >= 6 and "?" not in cleaned[:3]:
                five_way.record_adaptive_memory(
                    content=cleaned[:240],
                    category="conversation_memory",
                    topic=intent or "conversation",
                    title=cleaned[:50],
                    source="conversation_derived",
                )
        except Exception:
            pass

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
        async for ev in self.brain.stream_events(msgs):
            if ev.get("type") == "token" and ev.get("text"):
                parts.append(str(ev["text"]))
        return "".join(parts).strip()

    async def _iter_tool_turn(
        self,
        msgs: list[dict],
        *,
        tools: list[dict] | None,
        speak: bool,
        cancel_event: asyncio.Event | None,
        source: str,
        task_id: str | None,
        original_text: str,
        turn_ctx: dict,
    ) -> AsyncIterator[dict]:
        """Unified multi-round Brain + tool loop for both Local and Online modes with clean fallback."""
        gw = getattr(self.orchestrator, "gateway", None)
        active_prov = getattr(self.settings.providers, "active_provider", "ollama") or "ollama"
        use_gateway = gw is not None and active_prov != "ollama"

        if use_gateway or not tools:
            out_buf: list[str] = []
            async for ev in self._stream_llm_with_tts(msgs, speak=speak, cancel_event=cancel_event, answer_out=out_buf):
                yield ev
                if ev["type"] == "interrupted":
                    turn_ctx["interrupted"] = True
                    return
            turn_ctx["answer"] = out_buf[-1] if out_buf else ""
            return

        cur_msgs = list(msgs)
        chunker = StreamingSpeechBuffer()
        tools_supported = True
        max_rounds = 4
        answer = ""

        for round_idx in range(max_rounds):
            if cancel_event and cancel_event.is_set():
                self.speaker.stop()
                yield {"type": "interrupted"}
                turn_ctx["interrupted"] = True
                return

            round_tools = tools if round_idx < max_rounds - 1 else None
            round_answer = ""
            round_calls: list[dict] = []
            try:
                async for ev in self.brain.stream_events(cur_msgs, tools=round_tools):
                    if cancel_event and cancel_event.is_set():
                        self.speaker.stop()
                        yield {"type": "interrupted"}
                        turn_ctx["interrupted"] = True
                        return
                    if ev["type"] == "token" and ev.get("text"):
                        piece = ev["text"]
                        round_answer += piece
                        yield {"type": "token", "text": piece}
                        if speak:
                            for sentence in chunker.feed(piece):
                                self.speaker.say_chunk(sentence, precleaned=True)
                    elif ev["type"] == "tool_calls" and ev.get("calls"):
                        round_calls = ev["calls"]
            except ToolsUnsupportedError:
                tools_supported = False
                turn_ctx["tools_supported"] = False
                if round_answer:
                    yield {"type": "reset_answer"}
                    if speak:
                        self.speaker.stop()
                chunker.reset()
                if turn_ctx.get("defer_unsupported_to_web"):
                    turn_ctx["answer"] = ""
                    return
                out_buf = []
                async for ev in self._stream_llm_with_tts(cur_msgs, speak=speak, cancel_event=cancel_event, answer_out=out_buf):
                    yield ev
                    if ev["type"] == "interrupted":
                        turn_ctx["interrupted"] = True
                        return
                turn_ctx["answer"] = out_buf[-1] if out_buf else ""
                return

            if not round_calls:
                answer = round_answer
                break

            if round_answer:
                yield {"type": "reset_answer"}
                if speak:
                    self.speaker.stop()
            chunker.reset()
            assistant_calls = [
                {"type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in round_calls
            ]
            cur_msgs = [*cur_msgs, {"role": "assistant", "content": "", "tool_calls": assistant_calls}]
            for c in round_calls[:3]:
                if cancel_event and cancel_event.is_set():
                    self.speaker.stop()
                    yield {"type": "interrupted"}
                    turn_ctx["interrupted"] = True
                    return
                q_str = str(c["arguments"].get("query") or c["arguments"].get("path") or c["arguments"].get("url") or "")
                t_status = "searching" if c["name"] in ("web_search", "fetch_webpage") else "executing"
                yield {"type": "tool", "status": t_status, "name": c["name"], "query": q_str}
                self._set_os_state("Executing", source=source, detail=c["name"])
                t_tool = time.perf_counter()
                graph_live.tool_invoked(self, c["name"], query=q_str)
                tool_out, srcs = await self._execute_tool(
                    c,
                    task_id=task_id,
                    turn_ctx=turn_ctx,
                    original_text=original_text,
                )
                ok_flag = '"error"' not in tool_out[:60] and '"blocked": true' not in tool_out
                graph_live.tool_used(self, c["name"], query=q_str, ok=ok_flag, duration_ms=(time.perf_counter() - t_tool) * 1000.0)
                turn_ctx.setdefault("tools_used", []).append(c["name"])
                if srcs:
                    turn_ctx.setdefault("web_sources", []).extend(srcs)
                if turn_ctx.get("permission_request"):
                    turn_ctx["tools_supported"] = tools_supported
                    turn_ctx["answer"] = ""
                    return
                cur_msgs.append({"role": "tool", "content": tool_out})
            self._set_os_state("Thinking", source=source)

        turn_ctx["tools_supported"] = tools_supported
        turn_ctx["answer"] = answer
        if speak and answer.strip():
            for sentence in chunker.flush():
                self.speaker.say_chunk(sentence, precleaned=True)

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

    async def _resolve_fs_target_arg(self, raw_path: str | None) -> str:
        """Resolve an explicit path, alias, registered project, known user folder, or default approved root."""
        raw = str(raw_path or "").strip().strip("\"'")
        if raw:
            resolved_alias = await self.orchestrator.fs_access.resolve_alias(raw)
            if resolved_alias:
                return str(resolved_alias["resolved_path"])
            low = raw.lower()
            if low in ("documents", "my documents"):
                return str(Path.home() / "Documents")
            if low in ("downloads", "my downloads"):
                return str(Path.home() / "Downloads")
            if low in ("desktop", "my desktop"):
                return str(Path.home() / "Desktop")
            if low in ("pictures", "my pictures"):
                return str(Path.home() / "Pictures")
            if low in ("workspace", "notes"):
                roots = list(getattr(self.settings.filesystem, "allowed_roots", []) or [])
                if roots:
                    return str(roots[0])
            return raw

        active_perms = [
            p
            for p in await self.orchestrator.fs_access.list_permissions(status="active")
            if p.get("scope") in ("session", "persistent")
        ]
        if active_perms:
            return str(active_perms[0]["canonical_folder_path"])
        aliases = await self.orchestrator.fs_access.list_aliases()
        if aliases:
            return str(aliases[0]["folder_path"])
        roots = list(getattr(self.settings.filesystem, "allowed_roots", []) or [])
        return str(roots[0]) if roots else str(self.settings.notes_path)

    async def _execute_fs_tool(
        self,
        name: str,
        args: dict,
        *,
        task_id: str | None = None,
        turn_ctx: dict | None = None,
        original_text: str = "",
    ) -> str:
        """Execute a read-only, permission-gated filesystem tool and wrap untrusted outputs."""
        from jarvis.filesystem_access import READ_OPS
        from jarvis.security.untrusted import UNTRUSTED_DATA_POLICY, wrap_untrusted

        fs_access = self.orchestrator.fs_access
        raw_path = args.get("path") or args.get("folder") or ""
        if not raw_path and original_text:
            alias_hit = await fs_access.resolve_alias(original_text)
            if alias_hit:
                raw_path = str(alias_hit["resolved_path"])
        target = await self._resolve_fs_target_arg(str(raw_path or ""))

        op_map = {
            "fs_get_project_structure": "list_directory",
            "fs_list_directory": "list_directory",
            "fs_search_files": "search_files",
            "fs_find_files_by_extension": "search_files",
            "fs_read_file": "read_file",
            "fs_read_document": "read_file",
            "fs_read_metadata": "read_metadata",
        }
        req_op = op_map.get(name, "read_file")
        chk = await fs_access.authorize(target, req_op, task_id=task_id)
        if chk.blocked:
            return json.dumps({"error": chk.reason, "blocked": True, "code": chk.code, "path": chk.canonical_path}, ensure_ascii=False)
        if not chk.allowed:
            if turn_ctx is not None and not turn_ctx.get("permission_request"):
                req_ops = list(READ_OPS) if name in ("fs_get_project_structure", "fs_read_file", "fs_read_document") else [req_op, "list_directory", "read_metadata"]
                try:
                    req = await fs_access.create_permission_request(
                        target,
                        req_ops,
                        task_id=task_id,
                        reason=f"Tool `{name}` needs permission for `{target}`",
                        access_duration="once",
                        deferred_action={
                            "kind": "chat_turn_resume",
                            "tool": name,
                            "arguments": args,
                            "folder": target,
                            "original_text": original_text,
                        },
                    )
                    turn_ctx["permission_request"] = req
                except FilesystemSecurityError as exc:
                    return json.dumps({"error": str(exc), "blocked": True, "code": exc.code}, ensure_ascii=False)
            return json.dumps(
                {
                    "status": "permission_required",
                    "path": chk.canonical_path,
                    "message": "Owner permission required. Wait for owner decision via permission card.",
                },
                ensure_ascii=False,
            )

        try:
            if name == "fs_get_project_structure":
                res = await fs_access.execute_get_project_structure(
                    target,
                    task_id=task_id,
                    max_depth=int(args.get("max_depth") or 3),
                )
                if turn_ctx is not None:
                    turn_ctx.setdefault("paths", []).append(res["path"])
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=res["path"], files=[], operation="list_directory")
                wrapped = wrap_untrusted(res["tree"], source_type="local_file", source_id="project_tree", title=res["name"])
                return json.dumps(
                    {
                        "policy": UNTRUSTED_DATA_POLICY,
                        "path": res["path"],
                        "top_dirs": res["top_dirs"],
                        "top_files": res["top_files"],
                        "stack_markers": res["stack_markers"],
                        "truncated": res["truncated"],
                        "untrusted_tree": wrapped,
                    },
                    ensure_ascii=False,
                )

            if name == "fs_list_directory":
                entries = await fs_access.execute_list_directory(target, task_id=task_id)
                if turn_ctx is not None:
                    turn_ctx.setdefault("paths", []).extend(e["path"] for e in entries[:15])
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=target, files=entries, operation="list_directory")
                return json.dumps({"path": target, "count": len(entries), "entries": entries[:40]}, ensure_ascii=False)

            if name == "fs_search_files":
                query = str(args.get("query") or "").strip()
                matches = await fs_access.execute_search_files(target, query=query, task_id=task_id)
                if turn_ctx is not None:
                    turn_ctx.setdefault("paths", []).extend(m["path"] for m in matches[:15])
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=target, files=matches, operation="search_files")
                return json.dumps({"path": target, "query": query, "count": len(matches), "matches": matches[:25]}, ensure_ascii=False)

            if name == "fs_find_files_by_extension":
                exts = list(args.get("extensions") or [])
                matches = await fs_access.execute_find_files_by_extension(target, exts, task_id=task_id)
                if turn_ctx is not None:
                    turn_ctx.setdefault("paths", []).extend(m["path"] for m in matches[:15])
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=target, files=matches, operation="search_files")
                return json.dumps({"path": target, "extensions": exts, "count": len(matches), "matches": matches[:25]}, ensure_ascii=False)

            if name in ("fs_read_file", "fs_read_document"):
                if name == "fs_read_document":
                    doc = await fs_access.execute_read_document(target, task_id=task_id)
                else:
                    start_ln = int(args.get("start_line") or 1)
                    max_ln = int(args.get("max_lines") or 200)
                    doc = await fs_access.execute_read_file(
                        target,
                        task_id=task_id,
                        start_line=start_ln,
                        max_lines=max_ln,
                        max_bytes=28_000,
                    )
                if turn_ctx is not None:
                    turn_ctx.setdefault("paths", []).append(doc["path"])
                    if doc.get("redacted"):
                        turn_ctx.setdefault("warnings", []).append(f"Secrets redacted in {doc['name']}: {', '.join(doc.get('redacted_kinds') or [])}")
                    if doc.get("truncated"):
                        turn_ctx.setdefault("warnings", []).append(f"Truncated preview of {doc['name']} ({doc.get('total_lines')} total lines)")
                fitem = {"name": doc["name"], "path": doc["path"], "size_bytes": doc["size_bytes"]}
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=str(Path(doc["path"]).parent), files=[fitem], operation="read_file")
                return json.dumps(
                    {
                        "policy": UNTRUSTED_DATA_POLICY,
                        "path": doc["path"],
                        "name": doc["name"],
                        "encoding": doc.get("encoding"),
                        "total_lines": doc.get("total_lines"),
                        "truncated": doc.get("truncated"),
                        "redacted": doc.get("redacted"),
                        "redacted_kinds": doc.get("redacted_kinds"),
                        "doc_meta": doc.get("doc_meta"),
                        "untrusted_content": doc.get("untrusted_content"),
                    },
                    ensure_ascii=False,
                )

            if name == "fs_read_metadata":
                meta = await fs_access.execute_read_metadata(target, task_id=task_id)
                if turn_ctx is not None:
                    turn_ctx.setdefault("paths", []).append(meta["path"])
                return json.dumps(meta, ensure_ascii=False)
        except FilesystemSecurityError as exc:
            return json.dumps({"error": str(exc), "code": exc.code, "path": exc.path}, ensure_ascii=False)

        return json.dumps({"error": f"tool {name!r} is not permitted"})

    async def _execute_tool(
        self,
        call: dict,
        *,
        task_id: str | None = None,
        turn_ctx: dict | None = None,
        original_text: str = "",
    ) -> tuple[str, list[dict]]:
        """Execute ONLY approved backend web or scoped filesystem tools; wrap untrusted results strictly before returning to LLM."""
        from jarvis.security.untrusted import UNTRUSTED_DATA_POLICY, wrap_untrusted

        name = call.get("name", "")
        args = call.get("arguments") or {}
        if name.startswith("fs_"):
            out_json = await self._execute_fs_tool(
                name,
                args,
                task_id=task_id,
                turn_ctx=turn_ctx,
                original_text=original_text,
            )
            return out_json, []
        if name == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return json.dumps({"error": "empty query"}), []
            try:
                res = await self.search.search(query)
                results = res["results"]
                wrapped_items = [
                    wrap_untrusted(
                        r.get("snippet") or r.get("content") or "",
                        source_type="web_search",
                        source_id=f"search-{idx}",
                        url=str(r.get("url") or ""),
                        title=str(r.get("title") or ""),
                    )
                    for idx, r in enumerate(results, 1)
                ]
                payload = {
                    "policy": UNTRUSTED_DATA_POLICY,
                    "query": res["query"],
                    "provider": res["provider"],
                    "untrusted_results": wrapped_items,
                    "results": results,
                }
                return json.dumps(payload, ensure_ascii=False), results
            except SearchError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False), []
        if name == "fetch_webpage":
            url = str(args.get("url") or "").strip()
            try:
                page = await self.search.fetch_page(url)
                wrapped_content = wrap_untrusted(
                    page.get("content", ""),
                    source_type="web_page",
                    source_id=page.get("sha256", "")[:12] or "webpage",
                    url=str(page.get("url") or url),
                    title=str(page.get("title") or ""),
                    sha256=str(page.get("sha256") or ""),
                )
                payload = {
                    "policy": UNTRUSTED_DATA_POLICY,
                    "url": page["url"],
                    "title": page["title"],
                    "sha256": page.get("sha256", ""),
                    "untrusted_content": wrapped_content,
                }
                return json.dumps(payload, ensure_ascii=False), [
                    {"title": page["title"], "url": page["url"], "snippet": page["content"][:220], "provider": "webpage"}
                ]
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

    async def _synthesize_multistep_answer(
        self,
        goal: str,
        step_outputs: dict,
        paths: list[str],
        warnings: list[str],
    ) -> str:
        """Synthesize verified multi-step filesystem evidence through Brain with untrusted content boundaries."""
        from .security.untrusted import UNTRUSTED_DATA_POLICY, wrap_untrusted

        evidence_parts: list[str] = []
        for skey, val in step_outputs.items():
            if not val:
                continue
            if skey.startswith("step-1") and isinstance(val, dict) and val.get("tree"):
                evidence_parts.append(f"Structure ({val.get('path')}):\n{str(val['tree'])[:1600]}")
            elif skey.startswith("step-2") and isinstance(val, dict) and val.get("matches"):
                matched_names = ", ".join(m.get("name", "") for m in (val.get("matches") or [])[:12])
                evidence_parts.append(f"Matched files in {val.get('root')}: {matched_names}")
            elif isinstance(val, list):
                for item in val[:3]:
                    if isinstance(item, dict) and item.get("content"):
                        evidence_parts.append(
                            f"File: {item.get('path')}\n{str(item.get('content'))[:2800]}"
                        )

        evidence_blob = "\n\n".join(evidence_parts) if evidence_parts else f"Verified paths: {', '.join(paths[:10]) or 'none'}."
        wrapped = wrap_untrusted(
            evidence_blob,
            source_type="local_file",
            source_id="multistep_evidence",
        )
        today = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
        msgs = self._build_messages(
            self.history.window(),
            goal,
            [("Multi-step verified evidence", wrapped)],
            today,
            online=bool(self.settings.internet.enabled and audit.is_online_enabled()),
        )
        msgs.insert(1, {"role": "system", "content": UNTRUSTED_DATA_POLICY})
        try:
            ans = await self._brain_complete(msgs)
            if ans:
                return ans
        except BrainError:
            pass
        files_list = ", ".join(Path(p).name for p in paths[:8]) or "none"
        return f"Completed multi-step inspection of `{goal[:80]}`. Verified paths touched: {files_list}."

    async def _run_multistep_workflow(
        self,
        text: str,
        *,
        resolved_target: str | None,
        alias_name: str | None = None,
        task_id: str | None = None,
        source: str = "text",
        cancel_event: asyncio.Event | None = None,
        steps_override: list[dict] | None = None,
        resume_checkpoint: dict | None = None,
    ) -> dict:
        """Execute a decomposed multi-step task with DB checkpoints and permission pause/resume."""
        steps = list(steps_override or build_multistep_plan(text, resolved_target=resolved_target, alias_name=alias_name))
        if task_id and steps:
            await self.orchestrator.tasks.update_task(
                task_id,
                checkpoint_patch={"multistep_plan": steps, "resolved_target": resolved_target, "project_alias": alias_name},
            )
        self._set_os_state("Planning", source=source, detail=f"multi_step:{len(steps)}")
        res = await self.orchestrator.multistep.execute(
            goal=text,
            steps=steps,
            task_id=task_id,
            synthesize_fn=self._synthesize_multistep_answer,
            cancel_event=cancel_event,
            source=source,
            resume_checkpoint=resume_checkpoint,
        )
        if res.get("command") == "permission_required" and res.get("permission_request"):
            req = res["permission_request"]
            self._set_os_state("Waiting for approval", source=source, detail=req["canonical_path"])
            self.bus.publish({"type": "permission_requested", "request": req, "task_id": task_id})
        if alias_name:
            res["project_alias"] = alias_name
        if resolved_target:
            res["target_path"] = resolved_target
        return res

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

            if kind == "chat_turn_resume":
                tool_name = str(deferred.get("tool") or "fs_list_directory")
                tool_args = dict(deferred.get("arguments") or {})
                orig_text = str(deferred.get("original_text") or "")
                turn_ctx: dict = {"paths": [], "warnings": [], "web_sources": [], "tools_used": []}
                first_out, _ = await self._execute_tool(
                    {"name": tool_name, "arguments": tool_args},
                    task_id=task_id,
                    turn_ctx=turn_ctx,
                    original_text=orig_text,
                )
                turn_ctx["tools_used"].append(tool_name)
                if orig_text:
                    today = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
                    msgs = self._build_messages(
                        self.history.window(),
                        orig_text,
                        [],
                        today,
                        online=bool(self.settings.internet.enabled and audit.is_online_enabled()),
                    )
                    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": tool_name, "arguments": tool_args}}]})
                    msgs.append({"role": "tool", "content": first_out})
                    tools = _assistant_tools(include_fs=True, include_web=False, allow_page_fetch=False)
                    async for ev in self._iter_tool_turn(
                        msgs,
                        tools=tools,
                        speak=False,
                        cancel_event=None,
                        source="permission_resume",
                        task_id=task_id,
                        original_text=orig_text,
                        turn_ctx=turn_ctx,
                    ):
                        _ = ev
                    reply = str(turn_ctx.get("answer") or "").strip()
                else:
                    reply = first_out[:800]
                if not reply:
                    reply = f"Completed `{tool_name}` inside `{folder}`."
                if task_id:
                    await self.orchestrator.tasks.update_task(
                        task_id,
                        status="completed",
                        append_tool=tool_name,
                        generated_files=turn_ctx.get("paths") or [],
                        current_step="Completed approved filesystem inspection",
                        result=reply,
                    )
                    await self.orchestrator.fs_access.consume_task_once_permissions(task_id)
                return {
                    "command": "filesystem_ops",
                    "reply": reply,
                    "paths": turn_ctx.get("paths") or [],
                    "warnings": turn_ctx.get("warnings") or [],
                    "tools_used": turn_ctx.get("tools_used") or [],
                    "ok": True,
                }

            if kind == "multistep_resume":
                snap = await self.orchestrator.tasks.get_task(task_id) if task_id else None
                cp = dict((snap or {}).get("checkpoint_json") or (snap or {}).get("checkpoint") or {})
                return await self._run_multistep_workflow(
                    str(deferred.get("goal") or deferred.get("original_text") or (snap or {}).get("user_request") or ""),
                    resolved_target=str(deferred.get("folder") or folder or ""),
                    task_id=task_id,
                    steps_override=list(deferred.get("steps") or cp.get("multistep_plan") or []),
                    resume_checkpoint=cp,
                )

            if kind == "fs_read":
                target_path = str(deferred.get("target") or folder)
                doc = await self.orchestrator.fs_access.execute_read_file(target_path, task_id=task_id)
                fitem = {"name": doc["name"], "path": doc["path"], "size_bytes": doc["size_bytes"]}
                graph_live.filesystem_activity(self, task_id=task_id, folder_path=str(Path(doc["path"]).parent), files=[fitem], operation="read_file")
                orig_text = str(deferred.get("original_text") or "")
                notices: list[str] = []
                if doc.get("redacted"):
                    notices.append(f"secrets redacted: {', '.join(doc.get('redacted_kinds') or [])}")
                if doc.get("truncated"):
                    notices.append("content truncated to safe preview")
                notice_txt = f" [{' ; '.join(notices)}]" if notices else ""
                reply = f"Read `{doc['path']}` ({doc['size_bytes']} bytes){notice_txt}: {doc['content'][:300]}"
                if orig_text and re.search(r"\b(?:explain|summarize|what\s+is|analyze|describe)\b", orig_text, re.I):
                    try:
                        today = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
                        synth = await self._brain_complete(
                            self._build_messages(
                                [],
                                f"{orig_text}\n\nFile: {doc['path']}{notice_txt}",
                                [],
                                today,
                                untrusted_documents=[{"name": doc["name"], "path": doc["path"], "content": doc["content"], "sha256": doc.get("sha256", "")}],
                            )
                        )
                        if synth:
                            reply = synth + (f"\n\nNotice: {'; '.join(notices)}." if notices else "")
                    except Exception:
                        pass
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
                await self.orchestrator.command_history.update_by_task(
                    task_id,
                    status="cancelled",
                    result_summary=reply,
                    error_info="Access denied by owner",
                )
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
        if task_id:
            await self.orchestrator.command_history.update_by_task(
                task_id,
                status=str((task or {}).get("status") or ("completed" if exec_res.get("ok", True) else "failed")),
                tools_used=exec_res.get("tools_used"),
                paths_touched=exec_res.get("paths") or [],
                result_summary=reply,
            )
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
            fmt_map = {"word": "docx", "powerpoint": "pptx", "excel": "xlsx", "spreadsheet": "xlsx", "markdown": "md"}
            fmt = fmt_map.get(fmt_raw, fmt_raw if fmt_raw in {"pdf", "docx", "pptx", "xlsx", "md"} else "pdf")
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
            alias_hit = await self.orchestrator.fs_access.resolve_alias(text)
            if alias_hit and not fs_info["explicit_path"]:
                fs_info["target"] = str(alias_hit["resolved_path"])
                fs_info["is_workspace_only"] = False
                fs_info["unspecified_folder"] = False
            task_cat = classify_task_category(text, intent=intent, department=plan.decision.department)
            has_explicit_seq = bool(re.search(r"\b(?:and\s+then|then|after\s+that|followed\s+by)\b|;|->", text, re.I))
            if (
                task_cat == "multi_step"
                and has_explicit_seq
                and fs_info["primary_op"] not in WRITE_OPS
                and fs_info["primary_op"] != "delete_file"
                and not fs_info["is_workspace_only"]
                and (alias_hit or fs_info["explicit_path"] or not fs_info["unspecified_folder"])
            ):
                return await self._run_multistep_workflow(
                    text,
                    resolved_target=fs_info["target"],
                    alias_name=(alias_hit["alias"] if alias_hit else None),
                    task_id=task_id,
                    source=source,
                    cancel_event=cancel_event,
                )
            # Analytical / multi-step read-only inspection queries benefit from Brain multi-tool exploration + synthesis.
            if (
                fs_info["primary_op"] not in WRITE_OPS
                and fs_info["primary_op"] != "delete_file"
                and re.search(r"\b(?:explain|summarize|analyze|what\s+is|how\s+does|database\s+connection|config(?:uration)?)\b", text, re.I)
            ):
                return None
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

        if intent == "web_research" and re.search(
            r"\b(?:deep\s+research|deep\s+dive|comprehensive\s+research|verified\s+report)\b",
            text,
            re.I,
        ):
            self._set_os_state("Delegating", source=source, detail="deep_research")
            self._set_os_state("Executing", source=source, detail="deep_research")
            t_start = time.perf_counter()
            graph_live.tool_invoked(self, "deep_research", query=text[:80])
            res = await self.orchestrator.research_engine.run(
                task_id=task_id or f"dr-{int(time.time())}",
                question=text,
                export_format="md",
            )
            dur_ms = (time.perf_counter() - t_start) * 1000.0
            graph_live.tool_used(self, "deep_research", query=text[:80], ok=bool(res.get("ok")), duration_ms=dur_ms)
            if task_id:
                art_out = res.get("outcome")
                export_ok = bool(res.get("export_ok", True))
                await self.orchestrator.tasks.update_task(
                    task_id,
                    status="completed",
                    append_tool="deep_research",
                    generated_files=res.get("generated_files") or [],
                    result=res.get("markdown") or res.get("summary") or "",
                    error_info=(f"Artifact generation failed: {res.get('export_error')}" if not export_ok else None),
                    current_step=("Verified deep research ready [artifact export failed]" if not export_ok else "Verified deep research report ready"),
                    meta_patch={
                        "artifact_outcome": art_out,
                        "artifact_status": (art_out or {}).get("status") if isinstance(art_out, dict) else None,
                        "partial_failure": not export_ok,
                        "research_verification": res.get("verification"),
                    },
                )
            return {
                "command": "deep_research",
                "reply": str(res.get("markdown") or res.get("summary") or "Deep research finished."),
                "paths": list(res.get("generated_files") or []),
                "sources": res.get("sources") or [],
                "verification": res.get("verification"),
                "outcome": res.get("outcome"),
                "export_ok": bool(res.get("export_ok", True)),
                "export_error": str(res.get("export_error") or ""),
                "ok": bool(res.get("ok")),
            }

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

        if (
            intent not in ("document_export", "reminder", "deep_research", "task_status", "memory_manage", "system_status", "docker_ops", "recovery_ops")
            and classify_task_category(text, intent=intent, department=plan.decision.department) == "multi_step"
            and bool(re.search(r"\b(?:and\s+then|then|after\s+that|followed\s+by)\b|;|->", text, re.I))
        ):
            fs_info = self._parse_host_fs_request(text)
            alias_hit = await self.orchestrator.fs_access.resolve_alias(text)
            resolved_target: str | None = None
            alias_name: str | None = None
            if alias_hit and not fs_info["explicit_path"]:
                resolved_target = str(alias_hit["resolved_path"])
                alias_name = str(alias_hit["alias"])
            elif not fs_info["is_workspace_only"] and not fs_info["unspecified_folder"] and fs_info.get("target"):
                resolved_target = str(fs_info["target"])
            if resolved_target and fs_info["primary_op"] not in WRITE_OPS and fs_info["primary_op"] != "delete_file":
                return await self._run_multistep_workflow(
                    text,
                    resolved_target=resolved_target,
                    alias_name=alias_name,
                    task_id=task_id,
                    source=source,
                    cancel_event=cancel_event,
                )

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

    async def stream_turn(
        self,
        text: str,
        speak: bool = False,
        source: str = "text",
        cancel_event: asyncio.Event | None = None,
        *,
        _in_replay: bool = False,
    ) -> AsyncIterator[dict]:
        """Yields {"type": "meta"|"token"|"tool"|"reset_answer"|"interrupted"|"done"|"error", ...}. The only place a turn is executed."""
        eff_cancel = cancel_event or asyncio.Event()
        async with self._turn_lock:
            self._active_turn_cancel = eff_cancel
            try:
                async for ev in self._stream_turn_unlocked(
                    text,
                    speak=speak,
                    source=source,
                    cancel_event=eff_cancel,
                    _in_replay=_in_replay,
                ):
                    yield ev
            finally:
                if self._active_turn_cancel is eff_cancel:
                    self._active_turn_cancel = None

    async def _stream_turn_unlocked(
        self,
        text: str,
        speak: bool = False,
        source: str = "text",
        cancel_event: asyncio.Event | None = None,
        *,
        _in_replay: bool = False,
    ) -> AsyncIterator[dict]:
        text = text.strip()
        eff_cancel = cancel_event or asyncio.Event()
        prev_replay = getattr(self, "_in_replay", False)
        self._in_replay = _in_replay
        if True:
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
                    await self.orchestrator.command_history.record(
                        text,
                        task_category="informational",
                        intent=ui_cmd["command"],
                        status="completed",
                        result_summary=reply,
                        replayable=True,
                        source=source,
                    )
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
                    if cmd["name"] == "repeat_last_command" and cmd.get("replay_text") and not _in_replay:
                        async for ev in self._stream_turn_unlocked(
                            cmd["replay_text"],
                            speak=speak,
                            source="replay",
                            cancel_event=eff_cancel,
                            _in_replay=True,
                        ):
                            yield ev
                        return
                    if cmd["name"] == "continue_task" and cmd.get("replay_text") and not _in_replay:
                        if cmd.get("resume_task"):
                            rt = cmd["resume_task"]
                            cp = dict(rt.get("checkpoint_json") or rt.get("checkpoint") or {})
                            res = await self._run_multistep_workflow(
                                rt["user_request"],
                                resolved_target=cp.get("resolved_target"),
                                alias_name=cp.get("project_alias"),
                                task_id=rt["task_id"],
                                source="continue",
                                cancel_event=eff_cancel,
                                steps_override=cp.get("multistep_plan"),
                                resume_checkpoint=cp,
                            )
                            latest_task = await self.orchestrator.tasks.get_task(rt["task_id"])
                            reply = str(res.get("reply") or "Continued previous task.")
                            await self.orchestrator.command_history.update_by_task(
                                rt["task_id"],
                                status=str((latest_task or {}).get("status") or ("completed" if res.get("ok", True) else "failed")),
                                tools_used=res.get("tools_used"),
                                paths_touched=res.get("paths") or [],
                                result_summary=reply,
                            )
                            yield {"type": "meta", "command": res.get("command", "multi_step"), "nodes": [], "model": None, "web_sources": [], "task": latest_task, "task_id": rt["task_id"], "permission_request": res.get("permission_request")}
                            yield {"type": "token", "text": reply}
                            self.history.add("user", text)
                            self.history.add("assistant", reply)
                            yield {"type": "done", "answer": reply, "nodes": [], "paths": res.get("paths") or [], "web_sources": [], "command": res.get("command", "multi_step"), "task": latest_task, "task_id": rt["task_id"], "permission_request": res.get("permission_request"), "ok": bool(res.get("ok", True)), "seconds": round(time.time() - t0, 2)}
                            return
                        async for ev in self._stream_turn_unlocked(
                            cmd["replay_text"],
                            speak=speak,
                            source="continue",
                            cancel_event=eff_cancel,
                            _in_replay=True,
                        ):
                            yield ev
                        return
                    self._set_os_state("Executing", source=source, detail=cmd["name"])
                    if cmd["name"] in {
                        "search_files", "read_file", "open_file", "open_app", "inspect_project",
                        "knowledge_sync", "learning_status", "prepare_dataset", "training_request", "evaluate_model",
                    }:
                        graph_live.tool_used(self, cmd["name"], query=text[:80], ok=cmd.get("ok", True))
                    reply = cmd["reply"]
                    await self.orchestrator.command_history.record(
                        text,
                        task_category=classify_task_category(text, intent=cmd["name"]),
                        intent=cmd["name"],
                        status=("completed" if cmd.get("ok", True) else "failed"),
                        paths_touched=cmd.get("paths") or ([cmd["note"]] if cmd.get("note") else []),
                        result_summary=reply,
                        replayable=bool(cmd.get("replayable", True)),
                        source=source,
                    )
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
                await self.orchestrator.command_history.record(
                    text,
                    task_category=classify_task_category(text, intent=plan.decision.intent, department=plan.decision.department),
                    intent=plan.decision.intent,
                    department=plan.decision.department,
                    task_id=current_task_id,
                    status="running",
                    replayable=(plan.decision.intent != "task_status"),
                    source=source,
                )
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
                                await self.orchestrator.command_history.update_by_task(
                                    current_task_id,
                                    status="failed",
                                    result_summary=reply,
                                    error_info=chk_del.reason,
                                )
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
                    await self.orchestrator.command_history.update_by_task(
                        current_task_id,
                        status="awaiting_approval",
                        result_summary=reply,
                        error_info=f"Owner approval required ({flags})",
                    )
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
                        await self.orchestrator.command_history.update_by_task(current_task_id, status="cancelled", error_info="Interrupted by user")
                        self.speaker.stop()
                        yield {"type": "interrupted"}
                        return
                    reply = planned["reply"]
                    cmd_name = planned["command"]
                    eff_cmd_status = "awaiting_permission" if cmd_name == "permission_required" else ("awaiting_approval" if (cmd_name == "approval_required" or planned.get("approval_token")) else "completed")
                    err_detail = None
                    if cmd_name not in ("permission_required", "approval_required") and not (
                        cmd_name == "dev_autonomous" and planned.get("approval_token")
                    ):
                        planned_outcome = planned.get("outcome") if isinstance(planned.get("outcome"), dict) else None
                        planned_out_status = str((planned_outcome or {}).get("status") or "")
                        if cmd_name == "filesystem_blocked" or (not planned.get("ok", True) and cmd_name in ("document_export", "dev_autonomous")):
                            eff_status = "failed"
                        elif cmd_name != "deep_research" and planned_out_status in ("failed", "unverified", "started", "cancelled"):
                            eff_status = planned_out_status
                        else:
                            eff_status = "completed"
                        eff_cmd_status = eff_status
                        if cmd_name == "deep_research" and planned.get("export_ok") is False:
                            err_detail = f"Artifact generation failed: {planned.get('export_error')}"
                        await self.orchestrator.tasks.update_task(
                            current_task_id,
                            status=eff_status,
                            result=reply,
                            generated_files=planned.get("paths") or [],
                            error_info=err_detail,
                            current_step=f"Finished {cmd_name}" if eff_status == "completed" else f"{cmd_name}: {eff_status}",
                            meta_patch=({"outcome": planned_outcome, "outcome_status": planned_out_status} if planned_outcome and cmd_name != "deep_research" else None),
                        )
                        await self.orchestrator.fs_access.consume_task_once_permissions(current_task_id)
                    elif planned.get("approval_token"):
                        await self.orchestrator.tasks.update_task(
                            current_task_id,
                            status="awaiting_approval",
                            append_permission=planned.get("approval_token"),
                            current_step="Waiting for owner approval",
                        )
                    await self.orchestrator.command_history.update_by_task(
                        current_task_id,
                        status=eff_cmd_status,
                        tools_used=planned.get("tools_used"),
                        paths_touched=planned.get("paths") or [],
                        result_summary=reply,
                        error_info=err_detail,
                    )
                    if eff_cmd_status == "completed":
                        self._record_adaptive_turn_memory(
                            text,
                            cmd_name=planned["command"],
                            intent=plan.decision.intent,
                            paths=planned.get("paths") or [],
                            project_alias=planned.get("project_alias"),
                            ok=bool(planned.get("ok", True)),
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

                isolate_web = bool(
                    online
                    and (
                        plan.decision.intent in ("web_research", "research")
                        or getattr(plan.decision, "needs_live_web", False)
                    )
                )
                msgs = self._build_messages(
                    self.history.window(),
                    text,
                    [] if isolate_web else notes_ctx,
                    today,
                    online=online,
                    memories=None if isolate_web else (ctx_bundle["memories_ctx"] or None),
                    history_hits=None if isolate_web else (ctx_bundle["history_ctx"] or None),
                    feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                    knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                    isolate_untrusted_context=isolate_web,
                )
                include_fs = bool(getattr(self.orchestrator, "fs_access", None)) and not isolate_web
                include_web = bool(online and self.settings.internet.tool_calling)
                tools = _assistant_tools(
                    include_fs=include_fs,
                    include_web=include_web,
                    allow_page_fetch=self.settings.internet.allow_page_fetch,
                ) or None

                turn_ctx: dict = {
                    "answer": "",
                    "paths": [],
                    "warnings": [],
                    "web_sources": [],
                    "tools_used": [],
                    "tools_supported": tools is not None,
                    "defer_unsupported_to_web": bool(online),
                }
                async for ev in self._iter_tool_turn(
                    msgs,
                    tools=tools,
                    speak=speak,
                    cancel_event=eff_cancel,
                    source=source,
                    task_id=current_task_id,
                    original_text=text,
                    turn_ctx=turn_ctx,
                ):
                    yield ev
                    if turn_ctx.get("interrupted") or ev.get("type") == "interrupted":
                        return

                if turn_ctx.get("permission_request"):
                    req = turn_ctx["permission_request"]
                    updated_task = await self.orchestrator.tasks.update_task(
                        current_task_id,
                        status="awaiting_permission",
                        plan=[f"Request scoped permission on `{req['canonical_path']}`", "Resume inspection and synthesize explanation"],
                        append_permission=req["request_id"],
                        current_step=f"Waiting for permission ({', '.join(req.get('requested_operations') or [])})",
                    )
                    self._set_os_state("Waiting for approval", source=source, detail=req["canonical_path"])
                    self.bus.publish({"type": "permission_requested", "request": req, "task_id": current_task_id})
                    reply = f"Permission required to inspect `{req['canonical_path']}` ({', '.join(req.get('requested_operations') or [])}). Approve or deny in the inline permission card."
                    await self.orchestrator.command_history.update_by_task(
                        current_task_id,
                        status="awaiting_permission",
                        tools_used=turn_ctx.get("tools_used"),
                        result_summary=reply,
                    )
                    yield {
                        "type": "meta",
                        "command": "permission_required",
                        "nodes": nodes,
                        "model": None,
                        "web_sources": [],
                        "decision": decision_summary,
                        "permission_request": req,
                        "task": updated_task,
                        "task_id": current_task_id,
                    }
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak:
                        self.speaker.say(reply)
                    yield {
                        "type": "done",
                        "answer": reply,
                        "nodes": nodes,
                        "paths": [],
                        "web_sources": [],
                        "command": "permission_required",
                        "permission_request": req,
                        "task": updated_task,
                        "task_id": current_task_id,
                        "ok": False,
                        "seconds": round(time.time() - t0, 2),
                    }
                    return

                answer = str(turn_ctx.get("answer") or "")
                web_sources = list(turn_ctx.get("web_sources") or [])
                tools_supported = bool(turn_ctx.get("tools_supported", True))
                used_tools = list(turn_ctx.get("tools_used") or [])

                if online and not used_tools and (not tools_supported or not answer or needs_web_intent(text, has_notes=bool(hits))):
                    q = extract_web_query(text)
                    if answer:
                        yield {"type": "reset_answer"}
                    answer = ""
                    yield {"type": "tool", "status": "searching", "name": "web_search", "query": q}
                    self._set_os_state("Executing", source=source, detail="web_search")
                    t_tool = time.perf_counter()
                    graph_live.tool_invoked(self, "web_search", query=q)
                    try:
                        res = await self.search.search(q)
                        graph_live.tool_used(self, "web_search", query=q, ok=True, duration_ms=(time.perf_counter() - t_tool) * 1000.0)
                        self._set_os_state("Thinking", source=source)
                        web_sources = res["results"]
                        msgs = self._build_messages(
                            self.history.window(),
                            text,
                            [] if isolate_web else notes_ctx,
                            today,
                            online=True,
                            web_results=web_sources,
                            memories=None if isolate_web else (ctx_bundle["memories_ctx"] or None),
                            history_hits=None if isolate_web else (ctx_bundle["history_ctx"] or None),
                            feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                            knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                            isolate_untrusted_context=isolate_web,
                        )
                    except SearchError as e:
                        msgs = self._build_messages(
                            self.history.window(),
                            text,
                            [] if isolate_web else notes_ctx,
                            today,
                            online=True,
                            web_results=[{"title": "Web search error", "url": "", "snippet": str(e)}],
                            memories=None if isolate_web else (ctx_bundle["memories_ctx"] or None),
                            history_hits=None if isolate_web else (ctx_bundle["history_ctx"] or None),
                            feedback_lessons=ctx_bundle["feedback_ctx"] or None,
                            knowledge_hits=ctx_bundle["knowledge_ctx"] or None,
                            isolate_untrusted_context=isolate_web,
                        )
                    out_buf: list[str] = []
                    async for ev in self._stream_llm_with_tts(msgs, speak=speak, cancel_event=eff_cancel, answer_out=out_buf):
                        yield ev
                        if ev["type"] == "interrupted":
                            return
                    answer = out_buf[-1] if out_buf else ""

                answer = answer.strip()
                if not answer:
                    raise BrainError("The model returned an empty reply.")
                if turn_ctx.get("warnings"):
                    uniq_warn = list(dict.fromkeys(turn_ctx["warnings"]))
                    answer = f"{answer}\n\nSafety/Inspection Notice: {'; '.join(uniq_warn)}"
                self.history.add("user", text)
                self.history.add("assistant", answer)
                st = self.brain.last_stats
                # Deduplicate source URLs and verified file paths preserving order
                seen_urls = set()
                uniq_sources = []
                for s in web_sources:
                    u = s.get("url")
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        uniq_sources.append(s)
                all_paths = list(dict.fromkeys([n.path for n, _ in hits] + list(turn_ctx.get("paths") or [])))
                if current_task_id:
                    for tname in dict.fromkeys(used_tools):
                        await self.orchestrator.tasks.update_task(current_task_id, append_tool=tname)
                    await self.orchestrator.tasks.update_task(
                        current_task_id,
                        status="completed",
                        result=answer[:1000],
                        generated_files=list(dict.fromkeys(turn_ctx.get("paths") or [])),
                        current_step="Responded via LLM",
                    )
                    await self.orchestrator.fs_access.consume_task_once_permissions(current_task_id)
                    await self.orchestrator.command_history.update_by_task(
                        current_task_id,
                        status="completed",
                        tools_used=used_tools,
                        paths_touched=all_paths,
                        result_summary=answer,
                    )
                self._record_adaptive_turn_memory(
                    text,
                    cmd_name=(used_tools[0] if used_tools else ""),
                    intent=plan.decision.intent,
                    paths=all_paths,
                    ok=True,
                )
                latest_task = await self.orchestrator.tasks.get_task(current_task_id) if current_task_id else None
                yield {
                    "type": "done",
                    "answer": answer,
                    "nodes": nodes,
                    "paths": all_paths,
                    "memory_ids": ctx_bundle["memory_ids"],
                    "knowledge_sources": ctx_bundle["knowledge_sources"],
                    "web_sources": uniq_sources,
                    "tools_used": used_tools,
                    "warnings": turn_ctx.get("warnings") or [],
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
                    await self.orchestrator.command_history.update_by_task(current_task_id, status="failed", error_info=str(e))
                self._set_os_state("Error", source=source, detail=str(e))
                if speak:
                    self.speaker.say("I'm afraid I can't reach my language model, sir.")
                yield {"type": "error", "message": str(e)}
            finally:
                self._in_replay = prev_replay
                next_act = "speaking" if self.speaker.speaking else "idle"
                self._set_activity(next_act)
                if self.os_state not in ("Error", "Waiting for approval"):
                    rest = "Listening" if self.voice.mic_active else "Idle"
                    self._set_os_state("Speaking" if self.speaker.speaking else rest, source=source)

    async def chat(self, text: str, speak: bool = False, *, source: str = "text", _in_replay: bool = False) -> dict:
        last = {}
        async for ev in self.stream_turn(text, speak, source=source, _in_replay=_in_replay):
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
            "system": await asyncio.to_thread(sysinfo.snapshot, self.settings.monitoring, include_processes=False),
        }
