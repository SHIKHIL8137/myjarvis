"""FastAPI app. Loopback-only. Mutating requests need the X-Jarvis header (blocks cross-site form/fetch posts) and every
request must carry a loopback Host header (blocks DNS rebinding). Static viewer served from viewer/ only."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import audit, graph_live
from . import sysinfo
from .actions import ActionPermissionError
from .api_learning import create_learning_router
from .api_v2 import create_v2_router
from .api_workspace import create_workspace_router
from .assistant import Assistant
from .brain import BrainError
from .core.errors import ErrorEnvelope, JarvisError
from .core.logging_ctx import correlation_scope, sanitize_correlation_id
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

log = logging.getLogger("jarvis.api")
MAX_WAV = 10 * 1024 * 1024
# Successful mutations under these prefixes change the knowledge graph: tell live clients to diff a fresh snapshot.
GRAPH_MUTATION_PREFIXES = {
    "/api/v2/skills": "skill.updated",
    "/api/v2/projects": "project.updated",
    "/api/v2/documents/generate": "document.processed",
    "/api/v2/memory/experience": "knowledge.extracted",
    "/api/v2/memory/purge-expired": "memory.deleted",
    "/api/v2/memory/migrate-legacy": "memory.updated",
    "/api/v2/workforce": "agent.progress",
    "/api/v2/assistant": "agent.progress",
    "/api/v2/approvals": "agent.progress",
    "/api/v2/reminders": "task.updated",
    "/api/learning/knowledge": "knowledge.updated",
    "/api/memory/items": "memory.updated",
    "/api/memory/restore": "memory.updated",
}


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
    action: str = Field(..., pattern="^(start|pause|resume|stop|interrupt|mode|recover)$")
    mode: str | None = Field(None, max_length=20)


class CorrectIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    corrected_transcript: str = Field(min_length=1, max_length=8000)
    target_language: str | None = Field(None, max_length=16)
    eval_consent: bool | None = None


class TranslateIn(BaseModel):
    id: str | None = Field(None, max_length=64)
    text: str | None = Field(None, max_length=8000)
    source_language: str = Field("auto", max_length=16)
    target_language: str = Field(min_length=2, max_length=16)


class RetryVoiceIn(BaseModel):
    audio_id: str = Field(min_length=1, max_length=64)
    transcript_id: str | None = Field(None, max_length=64)
    model: str | None = Field(None, max_length=64)
    language: str | None = Field(None, max_length=16)
    quality: str | None = Field(None, max_length=16)
    preprocessing_profile: str | None = Field(None, max_length=16)
    target_language: str | None = Field(None, max_length=16)


class EvalConsentIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    consent: bool = True
    include_audio: bool = False


class InternetModeIn(BaseModel):
    enabled: bool


class ProviderKeyIn(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    api_key: str = Field(min_length=1, max_length=512)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int | None = Field(None, ge=1, le=10)


def sse(ev: dict) -> str:
    prefix = f"id: {ev['seq']}\n" if ev.get("seq") is not None else ""
    return f"{prefix}data: {json.dumps(ev, ensure_ascii=False)}\n\n"


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

    # ---- security + correlation + central errors -----------------------------------------------------
    def _err_resp(error: str, *, code: str, status_code: int, details: dict | None = None) -> JSONResponse:
        env = ErrorEnvelope(error=error, code=code, details=details or {})
        resp = JSONResponse(env.to_dict(include_detail=True), status_code=status_code)
        resp.headers["X-Correlation-ID"] = env.correlation_id
        return resp

    @app.middleware("http")
    async def guard(request: Request, call_next):
        corr_id = sanitize_correlation_id(request.headers.get("x-correlation-id"), prefix="req")
        with correlation_scope(correlation_id=corr_id, request_id=corr_id):
            host = request.headers.get("host", "")
            is_sandbox_gw = (
                request.url.path.startswith("/api/v2/sandbox/v1/")
                and not request.headers.get("origin")
                and (request.headers.get("authorization") or "").lower().startswith("bearer ")
            )
            sandbox_hosts = allowed_hosts | {f"host.docker.internal:{settings.port}"}
            if (host not in sandbox_hosts) if is_sandbox_gw else (host not in allowed_hosts):
                return _err_resp("forbidden host", code="forbidden_host", status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in allowed_hosts:
                return _err_resp("forbidden origin", code="forbidden_origin", status_code=403)
            if not is_sandbox_gw and request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("x-jarvis") != "1":
                return _err_resp("missing X-Jarvis header", code="csrf_header_required", status_code=403)
            resp = await call_next(request)
            if request.method != "GET" and resp.status_code < 400:
                for prefix, event in GRAPH_MUTATION_PREFIXES.items():
                    if request.url.path.startswith(prefix):
                        graph_live.emit(A().bus, event, resync=True)
                        break
            resp.headers["X-Correlation-ID"] = corr_id
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-eval'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'"
            )
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            if request.url.path.startswith("/api"):
                resp.headers["Cache-Control"] = "no-store"
            return resp

    @app.exception_handler(JarvisError)
    async def _jarvis_err(_, e: JarvisError):
        return _err_resp(str(e), code=e.code, status_code=e.status_code, details=e.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_err(_, e: StarletteHTTPException):
        msg = str(e.detail) if e.detail is not None else "HTTP error"
        code_map = {400: "bad_request", 403: "forbidden", 404: "not_found", 408: "timeout", 409: "conflict", 422: "validation_error", 429: "rate_limited", 503: "service_unavailable"}
        return _err_resp(msg, code=code_map.get(e.status_code, f"http_{e.status_code}"), status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def _req_val_err(_, e: RequestValidationError):
        errs = e.errors()
        msg = "; ".join(f"{'.'.join(str(x) for x in err.get('loc', []))}: {err.get('msg', 'invalid')}" for err in errs[:4]) or "validation error"
        return _err_resp(msg, code="validation_error", status_code=422, details={"errors": errs[:10]})

    @app.exception_handler(BrainError)
    async def _brain_err(_, e):
        return _err_resp(str(e), code="brain_unavailable", status_code=503)

    @app.exception_handler(SearchOfflineError)
    async def _offline_err(_, e):
        return _err_resp(str(e), code="search_offline", status_code=403)

    @app.exception_handler(SearchError)
    async def _search_err(_, e):
        return _err_resp(str(e), code="search_error", status_code=502)

    @app.exception_handler(KeyError)
    async def _nf(_, e):
        return _err_resp(f"not found: {e}", code="not_found", status_code=404)

    @app.exception_handler(ValueError)
    async def _bad(_, e):
        return _err_resp(str(e), code="validation_error", status_code=422)

    @app.exception_handler(ActionPermissionError)
    async def _perm(_, e):
        return _err_resp(str(e), code="permission_denied", status_code=403)

    @app.exception_handler(PermissionError)
    async def _perm2(_, e):
        return _err_resp(str(e), code="permission_denied", status_code=403)

    @app.exception_handler(FilesystemPermissionError)
    async def _fs_err(_, e):
        return _err_resp(str(e), code="filesystem_permission_denied", status_code=403)

    @app.exception_handler(FileNotFoundError)
    async def _fnf(_, e):
        return _err_resp(str(e), code="file_not_found", status_code=404)

    @app.exception_handler(Exception)
    async def _any(_, e):
        log.exception("unhandled error")
        return _err_resp("internal error (see logs/jarvis.log)", code="internal_error", status_code=500)

    # ---- health / status -----------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        h = await A().brain.health()
        return {"status": "ok" if h["up"] else "degraded", "ollama": h, "notes": len(A().store.all()), "mode": A().settings.mode}

    @app.get("/api/status")
    async def status():
        return await A().status()

    @app.get("/api/sysinfo")
    async def get_sysinfo(processes: bool = True, limit: int = 20):
        a = A()
        snap = await asyncio.to_thread(sysinfo.snapshot, a.settings.monitoring, include_processes=processes)
        recent_tasks = await a.orchestrator.tasks.list_tasks(limit=10)
        recent_cmds = await a.orchestrator.command_history.list_recent(limit=25)
        suggestions = a.proactive.collect(sys_snapshot=snap, recent_tasks=recent_tasks, recent_commands=recent_cmds)
        return {
            **snap,
            "history": sysinfo.monitor_history(limit=limit),
            "suggestions": suggestions,
            "jarvis": {
                "os_state": a.os_state,
                "mode": a.settings.mode,
                "active_brain": a.settings.boot_brain,
                "active_provider": a.settings.providers.active_provider,
                "queued_tasks": len([t for t in recent_tasks if t["status"] in ("queued", "running", "planning")]),
                "failed_tasks": len([t for t in recent_tasks if t["status"] == "failed"]),
                "voice_paused": a.voice.paused,
                "stt_ready": a.stt.ready,
                "tts_ready": a.tts.ready,
            },
        }

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
        graph_live.conversation_cleared(A())
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
        from .audio_preprocess import parse_wav_raw
        raw_arr, raw_sr, raw_ch = parse_wav_raw(data)
        a = A()
        stt_res = await asyncio.to_thread(
            a.stt.transcribe,
            raw_arr,
            sample_rate=raw_sr,
            channels=raw_ch,
            raw_wav_bytes=data,
            language=language,
            quality=quality,
        )
        rec = await a.translator.process_stt(stt_res, a.settings.translation, audio=raw_arr, target_override=translate_to)
        payload = rec.to_dict()
        a.bus.publish({"type": "transcript_record", **payload})
        return payload

    @app.post("/api/voice/command")
    async def voice_command(
        request: Request,
        speak: bool = Query(True),
        language: str | None = Query(None),
        quality: str | None = Query(None),
        translate_to: str | None = Query(None),
    ):
        """Accept WAV audio from UI mic/PTT, transcribe, translate/correct, and route through unified Assistant turn."""
        data = await request.body()
        if not data or len(data) > MAX_WAV:
            raise ValueError("body must be a WAV file of at most 10 MB")
        from .audio_preprocess import parse_wav_raw
        raw_arr, raw_sr, raw_ch = parse_wav_raw(data)
        audio = raw_arr
        a = A()
        a._set_os_state("Transcribing", source="voice", detail="whisper")
        stt_res = await asyncio.to_thread(
            a.stt.transcribe,
            raw_arr,
            sample_rate=raw_sr,
            channels=raw_ch,
            raw_wav_bytes=data,
            language=language,
            quality=quality,
        )
        raw_text = (stt_res.get("text") or "").strip()
        if raw_text:
            graph_live.publish_os_event(
                a.bus,
                "assistant.transcript.partial",
                legacy_type="transcript_partial",
                source_department="assistant",
                payload={"text": raw_text, "language": stt_res.get("detected_language", stt_res.get("language", "unknown"))},
                text=raw_text,
            )
        rec = await a.translator.process_stt(stt_res, a.settings.translation, audio=audio, target_override=translate_to)
        t_payload = rec.to_dict()
        a.bus.publish({"type": "transcript_record", **t_payload})
        prompt_text = (rec.translated_text if (rec.translated_text and rec.target_language == "en" and rec.detected_language != "en") else rec.corrected_transcript or "").strip()
        if not prompt_text:
            a._set_os_state("Idle", source="voice")
            return {"transcript": t_payload, "prompt": "", "ok": False, "answer": "", "ui": None}
        graph_live.publish_os_event(
            a.bus,
            "assistant.transcript.final",
            legacy_type="transcript_final",
            source_department="assistant",
            payload={"transcript_id": rec.id, "text": prompt_text, "language": rec.detected_language},
            text=prompt_text,
            transcript_id=rec.id,
        )
        a.bus.publish({"type": "user", "text": prompt_text, "transcript_id": rec.id, "source": "voice"})
        turn_events: list[dict] = []
        done_ev: dict = {}
        async for ev in a.stream_turn(prompt_text, speak=speak, source="voice"):
            a.bus.publish(ev)
            turn_events.append(ev)
            if ev.get("type") in ("done", "error", "interrupted"):
                done_ev = ev
        if done_ev.get("type") == "error":
            raise BrainError(done_ev.get("message") or "Turn failed")
        if done_ev.get("type") == "interrupted":
            return {"transcript": t_payload, "prompt": prompt_text, "ok": False, "interrupted": True, "answer": "", "ui": None}
        return {"transcript": t_payload, "prompt": prompt_text, "ok": done_ev.get("ok", True), "ui": done_ev.get("ui"), **done_ev}

    @app.post("/api/voice/correct")
    async def correct_transcript(body: CorrectIn):
        rec = await A().translator.apply_correction(
            body.id,
            body.corrected_transcript,
            target_language=body.target_language,
            eval_consent=body.eval_consent,
        )
        if rec is None:
            return JSONResponse({"error": f"Transcript '{body.id}' not found"}, 404)
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
        translated, verification = await a.translator.translate_text_with_verification(
            body.text,
            body.source_language,
            body.target_language,
            verify=bool(getattr(a.settings.translation, "verify_translations", True)),
            preserve_code_switching=bool(getattr(a.settings.translation, "preserve_mixed_code_switching", True)),
        )
        return {
            "source_text": body.text.strip(),
            "source_language": body.source_language,
            "target_language": body.target_language,
            "translated_text": translated,
            "verification": verification,
        }

    @app.get("/api/voice/transcripts")
    async def transcripts(limit: int = 20):
        return {"records": A().translator.records.list_recent(limit)}

    @app.get("/api/voice/audio/{audio_id}")
    async def voice_audio(audio_id: str):
        wav_bytes = A().stt.audio_store.get_wav_bytes(audio_id)
        if not wav_bytes:
            return JSONResponse({"error": f"Audio '{audio_id}' not found"}, 404)
        return Response(content=wav_bytes, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.get("/api/voice/diagnostics/{audio_id}")
    async def voice_diagnostics(audio_id: str):
        a = A()
        raw = a.stt.audio_store.get_raw(audio_id)
        if raw is None:
            return JSONResponse({"error": f"Audio '{audio_id}' not found"}, 404)
        raw_arr, raw_sr, raw_ch = raw
        from .audio_preprocess import diagnose_audio

        diag = diagnose_audio(
            raw_arr,
            sr=raw_sr,
            channels=raw_ch,
            profile=a.settings.stt.preprocessing_profile,
        )
        return {"audio_id": audio_id, "diagnostics": diag.to_dict()}

    @app.post("/api/voice/retry")
    async def retry_voice_transcript(body: RetryVoiceIn):
        a = A()
        raw = a.stt.audio_store.get_raw(body.audio_id)
        if raw is None:
            return JSONResponse({"error": f"Stored audio '{body.audio_id}' not found"}, 404)
        raw_arr, raw_sr, raw_ch = raw
        try:
            stt_res = await asyncio.to_thread(
                a.stt.transcribe,
                raw_arr,
                sample_rate=raw_sr,
                channels=raw_ch,
                language=body.language,
                quality=body.quality,
                model_override=body.model,
                preprocessing_profile=body.preprocessing_profile,
                save_audio=False,
            )
            import uuid
            stt_res["audio_id"] = body.audio_id
            stt_res["id"] = body.transcript_id or uuid.uuid4().hex[:16]
            rec = await a.translator.process_stt(
                stt_res,
                a.settings.translation,
                audio=raw_arr,
                target_override=body.target_language,
            )
            payload = rec.to_dict()
            a.bus.publish({"type": "transcript_record", **payload})
            return {"transcript_record": payload}
        except Exception as e:
            return JSONResponse({"error": f"retry transcription failed: {e}"}, 500)

    @app.post("/api/voice/eval-consent")
    async def save_voice_eval_consent(body: EvalConsentIn):
        a = A()
        rec = a.translator.records.get(body.id)
        if rec is None:
            return JSONResponse({"error": f"Transcript '{body.id}' not found"}, 404)
        rec.eval_consent = bool(body.consent)
        a.translator.records.put(rec)
        exported_path = None
        if rec.eval_consent:
            from .translator import export_consented_sample

            wav_bytes = a.stt.audio_store.get_wav_bytes(rec.audio_id) if (body.include_audio and rec.audio_id) else None
            exported_path = str(export_consented_sample(rec, a.settings.translation.eval_export_dir, audio_wav_bytes=wav_bytes))
        return {"ok": True, "eval_consent": rec.eval_consent, "exported_path": exported_path}

    @app.delete("/api/voice/transcripts/{record_id}")
    async def delete_voice_transcript(record_id: str, delete_audio: bool = Query(False)):
        a = A()
        rec = a.translator.records.get(record_id)
        if rec is None:
            return JSONResponse({"error": f"Transcript '{record_id}' not found"}, 404)
        if delete_audio and rec.audio_id:
            a.stt.audio_store.delete(rec.audio_id)
        ok = a.translator.records.delete(record_id)
        return {"ok": ok, "deleted_id": record_id}

    @app.delete("/api/voice/transcripts")
    async def clear_voice_transcripts(delete_audio: bool = Query(False)):
        a = A()
        count = a.translator.records.clear()
        if delete_audio:
            a.stt.audio_store.clear()
        return {"ok": True, "deleted_count": count}

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
            "mic_active": a.voice.mic_active,
            "wake_ready": a.wake.ready,
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
            await a.interrupt_all(reason="manual_interrupt")
        elif act == "recover":
            await asyncio.to_thread(a.voice.recover, mode=body.mode)
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
            "mic_active": a.voice.mic_active,
            "wake_ready": a.wake.ready,
            "duplex": a.voice.duplex_info(),
            "tts_status": a.speaker.status(),
            "error": a.voice.last_error,
        }

    @app.post("/api/voice/interrupt")
    async def voice_interrupt():
        return await A().interrupt_all(reason="manual_interrupt")

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
        from .api_deps import build_consolidated_settings
        return build_consolidated_settings(A())

    @app.put("/api/config")
    async def put_config(request: Request):
        from .api_deps import apply_settings_patch
        patch = await request.json()
        apply_settings_patch(A(), patch, allow_gateway_and_prefs=True)
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
    async def events(request: Request, since_seq: int = Query(0, ge=0)):
        a = A()
        header_id = request.headers.get("last-event-id")
        if since_seq == 0 and header_id and header_id.isdigit():
            since_seq = int(header_id)
        q = a.bus.subscribe()

        async def gen():
            try:
                missed, gap = ([], False)
                if since_seq > 0:
                    missed, gap = a.bus.events_since(since_seq)
                raw_os_state = getattr(a, "os_state", "Idle")
                safe_os_state = "Idle" if (raw_os_state == "Listening" and not a.voice.mic_active) else raw_os_state
                yield sse({
                    "type": "hello",
                    "schema_version": 1,
                    "seq": a.bus.latest_seq,
                    "resync_required": gap,
                    "activity": a.activity,
                    "assistant_state": safe_os_state,
                    "voice": a.voice.state,
                    "audio_state": a.voice.audio_state.value,
                    "voice_mode": a.voice.mode,
                    "paused": a.voice.paused,
                    "mic_suspended": a.voice.mic_suspended,
                    "mic_active": a.voice.mic_active,
                    "wake_ready": a.wake.ready,
                    "duplex": a.voice.duplex_info(),
                    "tts_status": a.speaker.status(),
                    "internet": a.search.status(),
                })
                if not gap:
                    for ev in missed:
                        yield sse(ev)
                while not await request.is_disconnected():
                    try:
                        yield sse(await asyncio.wait_for(q.get(), 15))
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                a.bus.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream")

    app.include_router(create_workspace_router())
    app.include_router(create_learning_router())
    app.include_router(create_v2_router())

    vdir = viewer or ROOT / "viewer"
    react_dist = ROOT / "apps" / "frontend" / "dist"
    if react_dist.exists() and (react_dist / "index.html").exists() and viewer is None:
        if vdir.exists():
            app.mount("/legacy", StaticFiles(directory=vdir, html=True), name="viewer_legacy")
        app.mount("/", StaticFiles(directory=react_dist, html=True), name="frontend")
    elif vdir.exists():
        app.mount("/", StaticFiles(directory=vdir, html=True), name="viewer")
    return app
