"""FastAPI app. Loopback-only. Mutating requests need the X-Jarvis header (blocks cross-site form/fetch posts) and every
request must carry a loopback Host header (blocks DNS rebinding). Static viewer served from viewer/ only."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit
from .actions import ActionPermissionError
from .api_learning import create_learning_router
from .api_workspace import create_workspace_router
from .assistant import Assistant
from .brain import BrainError
from .filesystem import FilesystemPermissionError
from .memory_store import infer_memory_category
from .config import (
    PROVIDER_ENV_VARS,
    ROOT,
    Settings,
    delete_provider_api_key,
    has_provider_api_key,
    load_settings,
    save_overrides,
    set_provider_api_key,
    setup_logging,
)
from .search import SearchError, SearchOfflineError
from .stt import SUPPORTED_LANGUAGES, list_local_models

log = logging.getLogger("jarvis.api")
MAX_WAV = 10 * 1024 * 1024


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    speak: bool = False


class SwitchIn(BaseModel):
    model: str = Field(min_length=1, max_length=100)


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class MemoryIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    category: str = Field("captures", max_length=30)
    title: str | None = Field(None, max_length=120)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class MicIn(BaseModel):
    enabled: bool
    mode: str | None = Field(None, max_length=20)


class VoiceSessionIn(BaseModel):
    action: str = Field(..., pattern="^(start|pause|resume|stop|interrupt|mode)$")
    mode: str | None = Field(None, max_length=20)


class CorrectIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    corrected_transcript: str = Field(min_length=1, max_length=8000)
    target_language: str | None = Field(None, max_length=16)


class TranslateIn(BaseModel):
    id: str | None = Field(None, max_length=64)
    text: str | None = Field(None, max_length=8000)
    source_language: str = Field("auto", max_length=16)
    target_language: str = Field(min_length=2, max_length=16)


class InternetModeIn(BaseModel):
    enabled: bool


class ProviderKeyIn(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    api_key: str = Field(min_length=1, max_length=512)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int | None = Field(None, ge=1, le=10)


def sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


def _api_keys_status() -> dict[str, bool]:
    return {p: has_provider_api_key(p) for p in PROVIDER_ENV_VARS}


def create_app(settings: Settings | None = None, assistant: Assistant | None = None, viewer: Path | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging()
    audit.install(enforce=settings.offline_enforced, online_enabled=settings.internet.enabled)
    allowed_hosts = {f"{h}:{settings.port}" for h in ("127.0.0.1", "localhost")} | {"testserver"}

    @asynccontextmanager
    async def lifespan(app):
        a = app.state.a
        await a.start()
        yield
        await a.stop()

    app = FastAPI(title="Jarvis", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.a = assistant or Assistant(settings)
    A = lambda: app.state.a  # noqa: E731

    # ---- security + central errors -------------------------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] not in allowed_hosts:
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("x-jarvis") != "1":
            return JSONResponse({"error": "missing X-Jarvis header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-eval'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(BrainError)
    async def _brain_err(_, e):
        return JSONResponse({"error": str(e)}, status_code=503)

    @app.exception_handler(SearchOfflineError)
    async def _offline_err(_, e):
        return JSONResponse({"error": str(e)}, status_code=403)

    @app.exception_handler(SearchError)
    async def _search_err(_, e):
        return JSONResponse({"error": str(e)}, status_code=502)

    @app.exception_handler(KeyError)
    async def _nf(_, e):
        return JSONResponse({"error": f"not found: {e}"}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad(_, e):
        return JSONResponse({"error": str(e)}, status_code=422)

    @app.exception_handler(ActionPermissionError)
    async def _perm(_, e):
        return JSONResponse({"error": str(e)}, status_code=403)

    @app.exception_handler(PermissionError)
    async def _perm2(_, e):
        return JSONResponse({"error": str(e)}, status_code=403)

    @app.exception_handler(FilesystemPermissionError)
    async def _fs_err(_, e):
        return JSONResponse({"error": str(e)}, status_code=403)

    @app.exception_handler(FileNotFoundError)
    async def _fnf(_, e):
        return JSONResponse({"error": str(e)}, status_code=404)

    @app.exception_handler(Exception)
    async def _any(_, e):
        log.exception("unhandled error")
        return JSONResponse({"error": "internal error (see logs/jarvis.log)"}, status_code=500)

    # ---- health / status -----------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        h = await A().brain.health()
        return {"status": "ok" if h["up"] else "degraded", "ollama": h, "notes": len(A().store.all()), "mode": A().settings.mode}

    @app.get("/api/status")
    async def status():
        return await A().status()

    # ---- chat ----------------------------------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(body: ChatIn):
        return await A().chat(body.message, body.speak)

    @app.post("/api/chat/stream")
    async def chat_stream(body: ChatIn):
        async def gen():
            async for ev in A().stream_turn(body.message, body.speak):
                yield sse(ev)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/history")
    async def history():
        return {"messages": A().history.items[-100:]}

    @app.delete("/api/history")
    async def clear_history():
        A().history.clear()
        return {"ok": True}

    # ---- models --------------------------------------------------------------------------------------
    @app.get("/api/models")
    async def models():
        return {"current": A().brain.current, "models": await A().brain.catalogue()}

    @app.post("/api/models/switch")
    async def switch(body: SwitchIn):
        info = await A().brain.switch(body.model)
        A().bus.publish({"type": "model", **info})
        A().warm_brain()
        return info

    # ---- voice & translation -------------------------------------------------------------------------
    @app.post("/api/voice/hear")
    async def hear(
        request: Request,
        language: str | None = Query(None),
        quality: str | None = Query(None),
        translate_to: str | None = Query(None),
    ):
        data = await request.body()
        if not data or len(data) > MAX_WAV:
            raise ValueError("body must be a WAV file of at most 10 MB")
        from .stt import wav_to_float
        audio = wav_to_float(data)
        a = A()
        stt_res = await asyncio.to_thread(a.stt.transcribe, audio, language=language, quality=quality)
        rec = await a.translator.process_stt(stt_res, a.settings.translation, audio=audio, target_override=translate_to)
        payload = rec.to_dict()
        a.bus.publish({"type": "transcript_record", **payload})
        return payload

    @app.post("/api/voice/correct")
    async def correct_transcript(body: CorrectIn):
        rec = await A().translator.apply_correction(body.id, body.corrected_transcript, target_language=body.target_language)
        payload = rec.to_dict()
        A().bus.publish({"type": "transcript_record", **payload})
        return payload

    @app.post("/api/voice/translate")
    async def translate(body: TranslateIn):
        a = A()
        if body.id:
            rec = a.translator.records.get(body.id)
            if rec is None:
                raise KeyError(body.id)
            updated = await a.translator.apply_correction(body.id, rec.corrected_transcript, target_language=body.target_language)
            payload = updated.to_dict()
            a.bus.publish({"type": "transcript_record", **payload})
            return payload
        if not body.text or not body.text.strip():
            raise ValueError("provide either 'id' or non-empty 'text'")
        translated = await a.translator.translate_text(body.text, body.source_language, body.target_language)
        return {
            "source_text": body.text.strip(),
            "source_language": body.source_language,
            "target_language": body.target_language,
            "translated_text": translated,
        }

    @app.get("/api/voice/transcripts")
    async def transcripts(limit: int = 20):
        return {"records": A().translator.records.list_recent(limit)}

    @app.post("/api/tts")
    async def tts(body: SpeakIn):
        if not A().tts.ready:
            return JSONResponse({"error": "Piper voice model not installed (see INSTALLATION.md)"}, status_code=503)
        wav = await asyncio.to_thread(A().tts.wav_bytes, body.text)
        return Response(wav, media_type="audio/wav", headers={"X-Jarvis-Voice": f"local:{A().settings.tts.voice}"})

    @app.post("/api/speak")
    async def speak(body: SpeakIn):
        return {"chunks": A().speaker.say(body.text)}

    @app.post("/api/speak/stop")
    async def speak_stop():
        A().speaker.stop()
        return {"ok": True}

    @app.post("/api/voice/mic")
    async def mic(body: MicIn):
        a = A()
        if body.enabled:
            a.voice.enable(mode=body.mode)
            for _ in range(60):  # wake model load + stream open; errors surface in the response
                await asyncio.sleep(0.1)
                if a.voice.state != "off" or a.voice.last_error:
                    break
        else:
            await asyncio.to_thread(a.voice.disable)
        return {
            "state": a.voice.state,
            "audio_state": a.voice.audio_state.value,
            "mode": a.voice.mode,
            "paused": a.voice.paused,
            "mic_suspended": a.voice.mic_suspended,
            "duplex": a.voice.duplex_info(),
            "tts_status": a.speaker.status(),
            "error": a.voice.last_error,
        }

    @app.post("/api/voice/session")
    async def voice_session(body: VoiceSessionIn):
        a = A()
        act = body.action
        if act == "start":
            a.voice.start_session(mode=body.mode)
            for _ in range(60):
                await asyncio.sleep(0.1)
                if a.voice.state != "off" or a.voice.last_error:
                    break
        elif act == "pause":
            a.voice.pause()
        elif act == "resume":
            a.voice.resume()
        elif act == "interrupt":
            a.voice.interrupt(reason="manual_interrupt")
        elif act == "mode":
            if not body.mode:
                raise ValueError("mode is required when action is 'mode'")
            a.voice.set_mode(body.mode)
        elif act == "stop":
            await asyncio.to_thread(a.voice.stop_session)
        return {
            "state": a.voice.state,
            "audio_state": a.voice.audio_state.value,
            "mode": a.voice.mode,
            "paused": a.voice.paused,
            "mic_suspended": a.voice.mic_suspended,
            "duplex": a.voice.duplex_info(),
            "tts_status": a.speaker.status(),
            "error": a.voice.last_error,
        }

    @app.post("/api/voice/interrupt")
    async def voice_interrupt():
        a = A()
        a.voice.interrupt(reason="manual_interrupt")
        return {
            "ok": True,
            "state": a.voice.state,
            "audio_state": a.voice.audio_state.value,
            "mode": a.voice.mode,
        }

    @app.post("/api/voice/ptt")
    async def ptt():
        a = A()
        a.voice.push_to_talk()
        return {"ok": True, "state": a.voice.state, "audio_state": a.voice.audio_state.value, "mode": a.voice.mode}

    @app.get("/api/audio/devices")
    async def devices():
        import sounddevice as sd
        return {"devices": [{"id": i, "name": d["name"], "in": d["max_input_channels"], "out": d["max_output_channels"]}
                            for i, d in enumerate(sd.query_devices()) if sd.query_hostapis(d["hostapi"])["name"] == "MME"]}

    # ---- internet & search ---------------------------------------------------------------------------
    @app.get("/api/internet")
    async def internet_status():
        a = A()
        return {
            **a.search.status(),
            "providers": sorted(a.search.providers),
            "api_keys_configured": _api_keys_status(),
            "audit_log": audit.external_log(30),
        }

    @app.post("/api/internet/mode")
    async def internet_mode(body: InternetModeIn):
        a = A()
        raw = a.settings.model_dump()
        raw["internet"]["enabled"] = body.enabled
        new = Settings.model_validate(raw)
        save_overrides({"internet": {"enabled": body.enabled}})
        a.apply_settings(new)
        st = a.search.status()
        a.bus.publish({"type": "internet", **st})
        return st

    @app.post("/api/internet/key")
    async def set_key(body: ProviderKeyIn):
        set_provider_api_key(body.provider, body.api_key)
        a = A()
        a.bus.publish({"type": "internet", **a.search.status()})
        return {"ok": True, "provider": body.provider.lower(), "configured": True}

    @app.delete("/api/internet/key/{provider}")
    async def clear_key(provider: str):
        delete_provider_api_key(provider)
        a = A()
        a.bus.publish({"type": "internet", **a.search.status()})
        return {"ok": True, "provider": provider.lower(), "configured": False}

    @app.post("/api/internet/search")
    async def web_search(body: SearchIn):
        return await A().search.search(body.query, body.max_results)

    # ---- memory / notes / graph ----------------------------------------------------------------------
    @app.post("/api/memory")
    async def memory(body: MemoryIn):
        from .retrieval import search
        a = A()
        related = [n.title for n, s in search(a.store.all(), body.text, 2) if s >= 5.0]
        note = await asyncio.to_thread(a.store.add, body.text, body.category, body.title, related)
        cat, top = infer_memory_category(body.text)
        a.memory_store.add(body.text, category=cat, topic=top, title=note.title, note_path=note.path)
        a.bus.publish({"type": "graph_changed", "new": [note.path]})
        return {"id": note.id, "title": note.title, "path": note.path, "category": note.category}

    @app.get("/api/memory/search")
    async def memory_search(q: str, k: int = 5):
        from .retrieval import search
        return {"results": [{"id": n.id, "title": n.title, "category": n.category, "path": n.path, "score": round(s, 2), "excerpt": n.excerpt[:200]}
                            for n, s in search(A().store.all(), q, max(1, min(k, 20)))]}

    @app.get("/api/notes")
    async def notes():
        return {"notes": [{"id": n.id, "title": n.title, "category": n.category, "path": n.path} for n in A().store.all()]}

    @app.get("/api/notes/{note_id}")
    async def note(note_id: int):
        n = A().store.get(note_id)
        if n is None:
            raise KeyError(note_id)
        return {"id": n.id, "title": n.title, "category": n.category, "path": n.path, "body": n.body}

    @app.put("/api/notes/{note_id}")
    async def note_update(note_id: int, body: NoteIn):
        n = await asyncio.to_thread(A().store.update, note_id, body.text)
        A().bus.publish({"type": "graph_changed"})
        return {"id": n.id, "title": n.title}

    @app.delete("/api/notes/{note_id}")
    async def note_delete(note_id: int):
        trash = await asyncio.to_thread(A().store.delete, note_id)
        A().bus.publish({"type": "graph_changed"})
        return {"moved_to": trash}

    @app.get("/api/graph")
    async def graph():
        return await asyncio.to_thread(A().store.graph)

    # ---- config --------------------------------------------------------------------------------------
    @app.get("/api/config")
    async def get_config():
        s = A().settings
        return {
            "tts": s.tts.model_dump(),
            "wake": s.wake.model_dump(),
            "audio": s.audio.model_dump(),
            "stt": s.stt.model_dump(),
            "translation": s.translation.model_dump(),
            "internet": s.internet.model_dump(exclude={"allowed_search_hosts"}),
            "memory": s.memory.model_dump(),
            "filesystem": s.filesystem.model_dump(),
            "actions": s.actions.model_dump(),
            "providers": s.providers.model_dump(),
            "learning": s.learning.model_dump(),
            "history_turns": s.history_turns,
            "boot_brain": s.boot_brain,
            "offline_enforced": s.offline_enforced,
            "mode": s.mode,
            "num_ctx": A().brain.spec.num_ctx,
            "voices": sorted(p.stem for p in (ROOT / "models" / "piper").glob("*.onnx")),
            "stt_models": list_local_models(),
            "stt_languages": SUPPORTED_LANGUAGES,
            "api_keys_configured": _api_keys_status(),
        }

    @app.put("/api/config")
    async def put_config(request: Request):
        patch = await request.json()
        allowed = {
            "tts", "wake", "audio", "stt", "translation", "internet",
            "history_turns", "boot_brain", "num_ctx",
            "memory", "filesystem", "actions", "providers", "learning",
        }
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError("only tts, wake, audio, stt, translation, internet, history_turns, boot_brain and num_ctx can be changed here")
        if "num_ctx" in patch:  # context size of the current brain; Ollama reloads the model with it on the next request
            from .config import BrainSpec
            b, n = A().brain, patch.pop("num_ctx")
            if not isinstance(n, int) or isinstance(n, bool) or not 1024 <= n <= 8192:
                raise ValueError("num_ctx must be an integer between 1024 and 8192 (4 GB VRAM)")
            spec = BrainSpec.model_validate({**b.spec.model_dump(), "num_ctx": n})
            if b.current in b.settings.brains:
                b.settings.brains[b.current] = spec
                save_overrides({"brains": {b.current: {"num_ctx": n}}})
            else:
                b.adhoc[b.current] = spec
        if "voice" in patch.get("tts", {}) and not (ROOT / "models" / "piper" / f"{patch['tts']['voice']}.onnx").exists():
            raise ValueError("that Piper voice is not installed in models/piper")
        if "model" in patch.get("stt", {}):
            local_models = list_local_models()
            if local_models and patch["stt"]["model"] not in local_models:
                raise ValueError(f"STT model {patch['stt']['model']!r} is not downloaded locally ({', '.join(local_models)})")
        if "api_key" in patch.get("internet", {}):
            raise ValueError("API keys must be configured via /api/internet/key, never in settings")
        raw = A().settings.model_dump()
        for k, v in patch.items():
            raw[k] = {**raw[k], **v} if isinstance(v, dict) else v
        new = Settings.model_validate(raw)  # raises ValueError/ValidationError on bad values -> 422
        save_overrides(patch)
        A().apply_settings(new)
        A().bus.publish({"type": "internet", **A().search.status()})
        return {"ok": True}

    @app.get("/api/drill/connections")
    async def drill_connections(since: float = 0):
        ev = audit.since(since)
        return {
            "all": ev[-200:],
            "remote": [e for e in ev if not e["local"]],
            "enforced": audit._state["enforce"],
            "online_enabled": audit.is_online_enabled(),
            "external_audit": audit.external_log(50),
        }

    @app.get("/api/events")
    async def events(request: Request):
        q = A().bus.subscribe()

        async def gen():
            try:
                yield sse({
                    "type": "hello",
                    "activity": A().activity,
                    "voice": A().voice.state,
                    "audio_state": A().voice.audio_state.value,
                    "voice_mode": A().voice.mode,
                    "paused": A().voice.paused,
                    "mic_suspended": A().voice.mic_suspended,
                    "duplex": A().voice.duplex_info(),
                    "tts_status": A().speaker.status(),
                    "internet": A().search.status(),
                })
                while not await request.is_disconnected():
                    try:
                        yield sse(await asyncio.wait_for(q.get(), 15))
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                A().bus.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream")

    app.include_router(create_workspace_router(app.state.a))
    app.include_router(create_learning_router(app.state.a))

    vdir = viewer or ROOT / "viewer"
    if vdir.exists():
        app.mount("/", StaticFiles(directory=vdir, html=True), name="viewer")
    return app
