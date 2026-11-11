"""The Assistant service: owns every component and runs a conversational turn (commands -> retrieval -> brain/web tools -> speech)."""
import asyncio
import datetime
import json
import logging
import re
import threading
import time
from typing import AsyncIterator

from . import audit, commands, sysinfo
from .aec import EchoReferenceBuffer, NLMSEchoCanceller
from .brain import Brain, BrainError, ToolsUnsupportedError
from .config import DATA, Settings
from .history import History
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
    def __init__(self, settings: Settings, data_dir=DATA, brain: Brain | None = None, search_service: WebSearchService | None = None):
        self.settings = settings
        audit.set_online_enabled(settings.internet.enabled)
        self.bus = EventBus()
        self.store = NoteStore(settings.notes_path)
        self.brain = brain or Brain(settings)
        self.history = History(data_dir / "history.jsonl", settings.history_turns)
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
        self._voice_task: asyncio.Task | None = None
        self._last_utt_id = 0
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

    def _on_speaker_state(self, speaking: bool):
        self.bus.publish({"type": "speaking", "on": speaking})
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
        """Live-apply changed settings (tts/stt/translation/wake/audio/internet/history)."""
        prev_voice_mode = self.settings.audio.voice_mode
        self.settings.tts = new.tts
        self.settings.stt = new.stt
        self.settings.translation = new.translation
        self.settings.wake = new.wake
        self.settings.audio = new.audio
        self.settings.internet = new.internet
        self.settings.history_turns = new.history_turns
        self.history.turns = new.history_turns
        self.tts.spec = new.tts
        self.tts._voice = None  # reload on next use (voice may have changed)
        self.stt.spec = new.stt
        self.warm_status["stt"] = self.stt.ready
        self.wake.spec = new.wake
        self.voice.s = self.settings
        if new.audio.voice_mode != prev_voice_mode:
            self.voice.set_mode(new.audio.voice_mode)
        else:
            self.voice.mode = new.audio.voice_mode
        self.search.spec = new.internet
        self.search.status_state = "offline" if not new.internet.enabled else "ready"
        audit.set_online_enabled(new.internet.enabled)

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
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._cancel_event.set)
        else:
            self._cancel_event.set()

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

        rec = await self.translator.process_stt(stt_res, self.settings.translation, audio=audio_f32)
        self.bus.publish({"type": "transcript_record", **rec.to_dict()})
        use_translated = rec.translated_text and rec.target_language == "en" and rec.detected_language != "en"
        prompt_text = (rec.translated_text if use_translated else rec.corrected_transcript or "").strip()
        if not prompt_text or self._cancel_event.is_set():
            return self._cancel_event.is_set()

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
        audio = self.stt.audio_store.get(stt_result.get("audio_id")) if stt_result.get("audio_id") else None
        rec = await self.translator.process_stt(stt_result, self.settings.translation, audio=audio)
        self.bus.publish({"type": "transcript_record", **rec.to_dict()})
        use_translated = rec.translated_text and rec.target_language == "en" and rec.detected_language != "en"
        prompt_text = (rec.translated_text if use_translated else rec.corrected_transcript or "").strip()
        if not prompt_text:
            return
        self.bus.publish({"type": "user", "text": prompt_text, "transcript_id": rec.id})
        async for ev in self.stream_turn(prompt_text, speak=True, source="voice"):
            self.bus.publish(ev)

    def _context(self, text: str):
        q = text
        if len(text.split()) < 4 and self.history.items:  # short follow-up: borrow the previous question's keywords
            prev = next((m["content"] for m in reversed(self.history.items) if m["role"] == "user"), "")
            q = f"{prev} {text}"
        hits = [(n, s) for n, s in search(self.store.all(), q, self.settings.context_notes) if s >= MIN_SCORE]
        return hits

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
        async with self._turn_lock:
            self.speaker.stop()  # new input interrupts any ongoing speech
            self._set_activity("thinking")
            t0 = time.time()
            try:
                if cancel_event and cancel_event.is_set():
                    yield {"type": "interrupted"}
                    return
                cmd = await commands.route(self, text)
                if cmd:
                    reply = cmd["reply"]
                    yield {"type": "meta", "command": cmd["name"], "nodes": cmd["nodes"], "model": None, "web_sources": []}
                    yield {"type": "token", "text": reply}
                    self.history.add("user", text)
                    self.history.add("assistant", reply)
                    if speak and cmd.get("speak", True):
                        self.speaker.say(reply)
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

                hits = self._context(text)
                nodes = [n.id for n, _ in hits]
                notes_ctx = [(n.title, n.excerpt[:self.settings.context_chars]) for n, _ in hits]
                today = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
                online = bool(self.settings.internet.enabled and audit.is_online_enabled())
                web_sources: list[dict] = []

                yield {
                    "type": "meta",
                    "nodes": nodes,
                    "model": self.brain.spec.model,
                    "sources": [n.title for n, _ in hits],
                    "online": online,
                    "web_sources": [],
                }

                # Local Mode (default): incremental sentence-level streaming LLM -> Piper TTS
                if not online:
                    msgs = build_messages(self.history.window(), text, notes_ctx, today, online=False)
                    answer, chunker = "", StreamingSpeechBuffer()
                    async for piece in self.brain.stream(msgs):
                        if cancel_event and cancel_event.is_set():
                            self.speaker.stop()
                            yield {"type": "interrupted"}
                            return
                        answer += piece
                        yield {"type": "token", "text": piece}
                        if speak:
                            for sentence in chunker.feed(piece):
                                self.speaker.say_chunk(sentence, precleaned=True)
                    if speak:
                        for sentence in chunker.flush():
                            self.speaker.say_chunk(sentence, precleaned=True)
                else:
                    # Online Assisted Mode: try Ollama tool calling first (if enabled), with deterministic fallback
                    msgs = build_messages(self.history.window(), text, notes_ctx, today, online=True)
                    tools = _web_tools(self.settings.internet.allow_page_fetch) if self.settings.internet.tool_calling else None
                    answer, chunker = "", StreamingSpeechBuffer()
                    tool_calls: list[dict] = []
                    tools_supported = tools is not None

                    if tools_supported:
                        try:
                            async for ev in self.brain.stream_events(msgs, tools=tools):
                                if cancel_event and cancel_event.is_set():
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
                            if cancel_event and cancel_event.is_set():
                                self.speaker.stop()
                                yield {"type": "interrupted"}
                                return
                            yield {"type": "tool", "status": "searching", "name": c["name"], "query": c["arguments"].get("query") or c["arguments"].get("url") or ""}
                            tool_out, srcs = await self._execute_tool(c)
                            web_sources.extend(srcs)
                            msgs.append({"role": "tool", "content": tool_out})
                        async for piece in self.brain.stream(msgs):
                            if cancel_event and cancel_event.is_set():
                                self.speaker.stop()
                                yield {"type": "interrupted"}
                                return
                            answer += piece
                            yield {"type": "token", "text": piece}
                            if speak:
                                for sentence in chunker.feed(piece):
                                    self.speaker.say_chunk(sentence, precleaned=True)
                        if speak:
                            for sentence in chunker.flush():
                                self.speaker.say_chunk(sentence, precleaned=True)
                    elif (not tools_supported or not answer) or needs_web_intent(text, has_notes=bool(hits)):
                        if not tools_supported or needs_web_intent(text, has_notes=bool(hits)):
                            q = extract_web_query(text)
                            if answer:
                                yield {"type": "reset_answer"}
                            answer = ""
                            chunker.reset()
                            yield {"type": "tool", "status": "searching", "name": "web_search", "query": q}
                            try:
                                res = await self.search.search(q)
                                web_sources = res["results"]
                                msgs = build_messages(self.history.window(), text, notes_ctx, today, online=True, web_results=web_sources)
                            except SearchError as e:
                                msgs = build_messages(
                                    self.history.window(),
                                    text,
                                    notes_ctx,
                                    today,
                                    online=True,
                                    web_results=[{"title": "Web search error", "url": "", "snippet": str(e)}],
                                )
                            async for piece in self.brain.stream(msgs):
                                if cancel_event and cancel_event.is_set():
                                    self.speaker.stop()
                                    yield {"type": "interrupted"}
                                    return
                                answer += piece
                                yield {"type": "token", "text": piece}
                                if speak:
                                    for sentence in chunker.feed(piece):
                                        self.speaker.say_chunk(sentence, precleaned=True)
                            if speak:
                                for sentence in chunker.flush():
                                    self.speaker.say_chunk(sentence, precleaned=True)
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
                yield {
                    "type": "done",
                    "answer": answer,
                    "nodes": nodes,
                    "paths": [n.path for n, _ in hits],
                    "web_sources": uniq_sources,
                    "command": None,
                    "ok": True,
                    "seconds": round(time.time() - t0, 2),
                    "ttft": round(st.get("ttft") or 0, 2),
                    "tps": round(st.get("tps", 0), 1),
                    "model": st.get("model"),
                    "truncated": st.get("truncated", False),
                }
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
        return {
            "ollama": h,
            "brain": {
                "key": self.brain.current,
                "model": self.brain.spec.model,
                "thinking": self.brain.spec.thinking,
                "num_ctx": self.brain.spec.num_ctx,
            },
            "voice": {
                "state": self.voice.state,
                "audio_state": self.voice.audio_state.value,
                "mode": self.voice.mode,
                "paused": self.voice.paused,
                "mic_suspended": self.voice.mic_suspended,
                "error": self.voice.last_error,
                "wake_enabled": self.settings.wake.enabled,
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
            "activity": self.activity,
            "tts": {"enabled": self.settings.tts.enabled, "voice": self.settings.tts.voice},
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
