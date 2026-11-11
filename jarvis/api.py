"""FastAPI app. Loopback-only. Mutating requests need the X-Jarvis header (blocks cross-site form/fetch posts) and every
request must carry a loopback Host header (blocks DNS rebinding). Static viewer served from viewer/ only."""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit
from .assistant import Assistant
from .brain import BrainError
from .config import ROOT, Settings, load_settings, save_overrides, setup_logging

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


def sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


def create_app(settings: Settings | None = None, assistant: Assistant | None = None, viewer: Path | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging()
    audit.install(enforce=settings.offline_enforced)
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
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self' 'unsafe-eval'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(BrainError)
    async def _brain_err(_, e):
        return JSONResponse({"error": str(e)}, status_code=503)

    @app.exception_handler(KeyError)
    async def _nf(_, e):
        return JSONResponse({"error": f"not found: {e}"}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad(_, e):
        return JSONResponse({"error": str(e)}, status_code=422)

    @app.exception_handler(Exception)
    async def _any(_, e):
        log.exception("unhandled error")
        return JSONResponse({"error": "internal error (see logs/jarvis.log)"}, status_code=500)

    # ---- health / status -----------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        h = await A().brain.health()
        return {"status": "ok" if h["up"] else "degraded", "ollama": h, "notes": len(A().store.all())}

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

    # ---- voice ---------------------------------------------------------------------------------------
    @app.post("/api/voice/hear")
    async def hear(request: Request):
        data = await request.body()
        if not data or len(data) > MAX_WAV:
            raise ValueError("body must be a WAV file of at most 10 MB")
        from .stt import wav_to_float
        audio = wav_to_float(data)
        return await asyncio.to_thread(A().stt.transcribe, audio)

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
            a.voice.enable()
            for _ in range(60):  # wake model load + stream open; errors surface in the response
                await asyncio.sleep(0.1)
                if a.voice.state != "off" or a.voice.last_error:
                    break
        else:
            await asyncio.to_thread(a.voice.disable)
        return {"state": a.voice.state, "error": a.voice.last_error}

    @app.post("/api/voice/ptt")
    async def ptt():
        A().voice.push_to_talk()
        return {"ok": True}

    @app.get("/api/audio/devices")
    async def devices():
        import sounddevice as sd
        return {"devices": [{"id": i, "name": d["name"], "in": d["max_input_channels"], "out": d["max_output_channels"]}
                            for i, d in enumerate(sd.query_devices()) if sd.query_hostapis(d["hostapi"])["name"] == "MME"]}

    # ---- memory / notes / graph ----------------------------------------------------------------------
    @app.post("/api/memory")
    async def memory(body: MemoryIn):
        from .retrieval import search
        a = A()
        related = [n.title for n, s in search(a.store.all(), body.text, 2) if s >= 5.0]
        note = await asyncio.to_thread(a.store.add, body.text, body.category, body.title, related)
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
        return {"tts": s.tts.model_dump(), "wake": s.wake.model_dump(), "audio": s.audio.model_dump(), "stt": s.stt.model_dump(),
                "history_turns": s.history_turns, "boot_brain": s.boot_brain, "offline_enforced": s.offline_enforced,
                "num_ctx": A().brain.spec.num_ctx, "voices": sorted(p.stem for p in (ROOT / "models" / "piper").glob("*.onnx"))}

    @app.put("/api/config")
    async def put_config(request: Request):
        patch = await request.json()
        if not isinstance(patch, dict) or set(patch) - {"tts", "wake", "audio", "history_turns", "boot_brain", "num_ctx"}:
            raise ValueError("only tts, wake, audio, history_turns, boot_brain and num_ctx can be changed here")
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
        raw = A().settings.model_dump()
        for k, v in patch.items():
            raw[k] = {**raw[k], **v} if isinstance(v, dict) else v
        new = Settings.model_validate(raw)  # raises ValueError/ValidationError on bad values -> 422
        save_overrides(patch)
        A().apply_settings(new)
        return {"ok": True}

    @app.get("/api/drill/connections")
    async def drill_connections(since: float = 0):
        ev = audit.since(since)
        return {"all": ev[-200:], "remote": [e for e in ev if not e["local"]], "enforced": audit._state["enforce"]}

    @app.get("/api/events")
    async def events(request: Request):
        q = A().bus.subscribe()

        async def gen():
            try:
                yield sse({"type": "hello", "activity": A().activity, "voice": A().voice.state})
                while not await request.is_disconnected():
                    try:
                        yield sse(await asyncio.wait_for(q.get(), 15))
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                A().bus.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream")

    vdir = viewer or ROOT / "viewer"
    if vdir.exists():
        app.mount("/", StaticFiles(directory=vdir, html=True), name="viewer")
    return app
