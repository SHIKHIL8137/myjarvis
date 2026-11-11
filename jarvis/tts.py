"""Text-to-speech: Piper (CPU, offline). Text cleaning, streaming sentence-boundary chunking,
disk cache, Bluetooth-resilient audio output, echo-reference tap, and ordered async synthesis/playback."""
import hashlib
import io
import logging
import queue
import re
import threading
import time
import wave

import numpy as np

from .config import DATA, ROOT, TTSSpec

log = logging.getLogger("jarvis.tts")

EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿️‍]+")
SENT = re.compile(r"(?<=[.!?।॥…])\s+")
_ABBR = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|Fig|No|v\d+(?:\.\d+)?)\.$", re.I)


def _strip_markdown_and_emoji(text: str) -> str:
    t = re.sub(r"`([^`]*)`", r"\1", text)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = EMOJI.sub("", t)
    t = re.sub(r"(?m)^\s*(#{1,6}|[-*+>]|\d+[.)])\s+", "", t)
    t = re.sub(r"[*_~#|]+", "", t)
    return t


def clean_for_speech(text: str) -> str:
    """Strip everything a human would not say: code, markdown, emoji, URLs."""
    had_code = "```" in text
    t = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    t = _strip_markdown_and_emoji(t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t).strip()
    if had_code:
        t = (t + " The code is on your screen.").strip()
    return t


def split_chunks(text: str, limit: int = 170) -> list[str]:
    """Pack whole sentences into chunks of about `limit` chars; never split inside a sentence unless one exceeds 2x limit."""
    out, cur = [], ""
    for s in SENT.split(text.strip()):
        while len(s) > limit * 2:  # pathological sentence: split at a comma/space
            cut = max(s.rfind(", ", 0, limit * 2), s.rfind(" ", 0, limit * 2))
            cut = cut if cut > 0 else limit * 2
            out.append((cur + " " + s[:cut]).strip()) if cur else out.append(s[:cut].strip())
            cur, s = "", s[cut:].strip(" ,")
        if cur and len(cur) + 1 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return [c for c in out if c]


def _is_sentence_boundary(buf: str, pos: int) -> bool:
    """True when punctuation at `pos` ends a natural sentence (ignores decimals like 3.14, URLs, and abbreviations)."""
    ch = buf[pos]
    nxt = buf[pos + 1] if pos + 1 < len(buf) else ""
    if ch in "!?" or ch in "।॥…":
        return not nxt or nxt.isspace() or nxt in '"\')]'
    if ch == ".":
        prev = buf[pos - 1] if pos > 0 else ""
        # Decimal or version number (3.14) or mid-word/domain (example.com)
        if prev.isdigit() and nxt.isdigit():
            return False
        if nxt and not (nxt.isspace() or nxt in '"\')]'):
            return False
        # Abbreviation check (e.g., Dr., Mr., v1.2.)
        if _ABBR.search(buf[: pos + 1]):
            return False
        return True
    return False


class StreamingSpeechBuffer:
    """Incremental LLM-to-TTS sentence chunker supporting English, Malayalam, and inline code suppression."""

    def __init__(self, min_chunk_chars: int = 20, soft_max_chars: int = 140, max_chunk_chars: int = 220):
        self.min_chunk_chars = min_chunk_chars
        self.soft_max_chars = soft_max_chars
        self.max_chunk_chars = max_chunk_chars
        self.reset()

    def reset(self):
        self._raw = ""
        self._in_code = False
        self._code_noted = False
        self._pending_prefix = ""

    def _consume_fences(self) -> list[str]:
        """Strip fenced ```...``` blocks while keeping surrounding prose in order."""
        out: list[str] = []
        while True:
            if not self._in_code:
                idx = self._raw.find("```")
                if idx < 0:
                    # Retain trailing partial backticks cleanly across tokens
                    keep = 2 if self._raw.endswith("``") else (1 if self._raw.endswith("`") else 0)
                    body = self._raw[: len(self._raw) - keep]
                    self._raw = self._raw[len(self._raw) - keep :]
                    if body:
                        out.append(body)
                    break
                if idx > 0:
                    out.append(self._raw[:idx])
                self._raw = self._raw[idx + 3 :]
                self._in_code = True
                self._code_noted = False
            else:
                idx = self._raw.find("```")
                if idx < 0:
                    # Wait until fence closes; trim huge code internals safely
                    if len(self._raw) > 16:
                        self._raw = self._raw[-4:]
                    break
                self._raw = self._raw[idx + 3 :]
                self._in_code = False
                if not self._code_noted:
                    out.append(" The code is on your screen. ")
                    self._code_noted = True
        return out

    def _clean_piece(self, s: str) -> str:
        t = _strip_markdown_and_emoji(s)
        t = re.sub(r"\n{2,}", ". ", t)
        t = re.sub(r"\s+", " ", t)
        return t

    def _emit_ready(self, flush: bool = False) -> list[str]:
        chunks: list[str] = []
        cur = self._pending_prefix
        while cur:
            # Find sentence boundaries in `cur`
            split_at = -1
            for i, ch in enumerate(cur):
                if ch in ".!?।॥…" and _is_sentence_boundary(cur, i):
                    cand = cur[: i + 1].strip()
                    if len(cand) >= self.min_chunk_chars:
                        split_at = i + 1
                        break
                    # Small opener (e.g. "Sure.") — peek ahead for next boundary up to soft_max_chars
                    if len(cur) > self.soft_max_chars:
                        split_at = i + 1
                        break
            if split_at > 0:
                piece = cur[:split_at].strip()
                cur = cur[split_at:].lstrip()
                if piece:
                    chunks.append(piece)
                continue

            # If a single unbroken sentence grows very long without punctuation, break on natural comma/clause
            if len(cur) >= self.max_chunk_chars:
                cut = -1
                for sep in (", ", "; ", " — ", " - ", " "):
                    pos = cur.rfind(sep, self.min_chunk_chars, self.max_chunk_chars)
                    if pos > 0:
                        cut = pos + (1 if sep != " " else 0)
                        break
                if cut <= 0:
                    cut = self.max_chunk_chars
                piece = cur[:cut].strip()
                cur = cur[cut:].lstrip(" ,;")
                if piece:
                    chunks.append(piece)
                continue

            break

        if flush:
            tail = cur.strip()
            if tail:
                chunks.append(tail)
            cur = ""
        self._pending_prefix = cur
        return [re.sub(r"\s+([.,!?;:])", r"\1", c).strip() for c in chunks if c and c.strip()]

    def feed(self, token: str) -> list[str]:
        if not token:
            return []
        self._raw += token
        prose_parts = self._consume_fences()
        if prose_parts:
            self._pending_prefix = self._clean_piece(self._pending_prefix + "".join(prose_parts))
        return self._emit_ready(flush=False)

    def flush(self) -> list[str]:
        if not self._in_code and self._raw:
            self._pending_prefix = self._clean_piece(self._pending_prefix + self._raw)
        elif self._in_code and not self._code_noted:
            self._pending_prefix = (self._pending_prefix + " The code is on your screen.").strip()
            self._code_noted = True
        self._raw = ""
        self._in_code = False
        return self._emit_ready(flush=True)


_VOICE_POOL: dict[str, tuple[object, threading.Lock]] = {}
_POOL_LOCK = threading.Lock()


class PiperTTS:
    def __init__(self, spec: TTSSpec):
        self.spec = spec
        self._voice = None
        self._lock = threading.Lock()
        self.cache_dir = DATA / "tts_cache"

    @property
    def ready(self) -> bool:
        return (ROOT / "models" / "piper" / f"{self.spec.voice}.onnx").exists()

    @classmethod
    def list_voices(cls) -> list[dict]:
        """Discover installed Piper ONNX voices and parse language/speaker/quality metadata."""
        import json as _json
        pdir = ROOT / "models" / "piper"
        if not pdir.exists():
            return []
        voices = []
        for p in sorted(pdir.glob("*.onnx")):
            stem = p.stem
            parts = stem.split("-")
            lang = parts[0] if len(parts) >= 1 else "unknown"
            speaker = parts[1] if len(parts) >= 2 else stem
            quality = parts[2] if len(parts) >= 3 else "standard"
            sr = 22050
            cfg_file = p.with_suffix(".onnx.json")
            if cfg_file.exists():
                try:
                    meta = _json.loads(cfg_file.read_text(encoding="utf-8"))
                    sr = int(meta.get("audio", {}).get("sample_rate") or sr)
                    lang_info = meta.get("language", {})
                    if isinstance(lang_info, dict) and lang_info.get("code"):
                        lang = lang_info["code"]
                except Exception:
                    pass
            voices.append({"name": stem, "language": lang, "speaker": speaker, "quality": quality, "sample_rate": sr, "ready": True})
        return voices

    def switch_voice(self, query: str) -> dict:
        """Select an installed Piper voice by exact name, speaker, or language substring."""
        q = (query or "").strip().lower()
        avail = self.list_voices()
        if not avail:
            raise FileNotFoundError("No Piper voices are installed under models/piper/")
        match = next((v for v in avail if v["name"].lower() == q), None)
        if not match:
            match = next((v for v in avail if q and (v["speaker"].lower() == q or q in v["name"].lower() or q in v["language"].lower())), None)
        if not match:
            raise KeyError(f"Voice {query!r} not found. Available: {', '.join(v['name'] for v in avail)}")
        with self._lock:
            self.spec.voice = match["name"]
            self._voice = None  # force clean load on next synthesis
        return {**match, "speed": self.spec.speed, "current": True}

    def set_rate(self, speed: float) -> float:
        if not 0.5 <= float(speed) <= 2.0:
            raise ValueError("Speaking rate must be between 0.5 and 2.0")
        self.spec.speed = round(float(speed), 2)
        return self.spec.speed

    def health(self) -> dict:
        return {"ready": self.ready, "voice": self.spec.voice, "speed": self.spec.speed, "enabled": self.spec.enabled, "installed_count": len(self.list_voices())}

    def load(self):
        with self._lock:
            voice_key = self.spec.voice
            with _POOL_LOCK:
                pooled = _VOICE_POOL.get(voice_key)
            if pooled is not None:
                self._voice = pooled[0]
                return self._voice
            if self._voice is None:
                from piper import PiperVoice
                v = PiperVoice.load(ROOT / "models" / "piper" / f"{voice_key}.onnx")
                with _POOL_LOCK:
                    if voice_key not in _VOICE_POOL:
                        _VOICE_POOL[voice_key] = (v, threading.Lock())
                    self._voice = _VOICE_POOL[voice_key][0]
                log.info("piper voice loaded: %s", voice_key)
        return self._voice

    def synth(self, text: str) -> tuple[int, np.ndarray]:
        """-> (sample_rate, int16 mono). Reuses loaded ONNX voice & caches short chunks on disk."""
        from piper import SynthesisConfig
        key = hashlib.sha1(f"{self.spec.voice}|{self.spec.speed}|{text}".encode()).hexdigest()
        f = self.cache_dir / f"{key}.wav"
        if f.exists():
            with wave.open(str(f)) as w:
                return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        v = self.load()
        with _POOL_LOCK:
            v_lock = _VOICE_POOL.get(self.spec.voice, (v, self._lock))[1]
        cfg = SynthesisConfig(length_scale=1.0 / self.spec.speed)
        parts, sr = [], 22050
        with v_lock:  # Piper voices are not thread-safe
            for ch in v.synthesize(text, syn_config=cfg):
                sr = ch.sample_rate
                parts.append(ch.audio_int16_array)
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(f), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
            w.writeframes(audio.tobytes())
        return sr, audio

    def wav_bytes(self, text: str) -> bytes:
        sr, audio = self.synth(clean_for_speech(text) or text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
            w.writeframes(audio.tobytes())
        return buf.getvalue()


def _resolve_output_device(sd, dev):
    """Resolve explicit int ID, device name substring, or None (system default), verifying channels > 0."""
    if dev is None or dev == "":
        return None
    if isinstance(dev, int):
        try:
            info = sd.query_devices(dev, "output")
            if info and info.get("max_output_channels", 0) > 0:
                return dev
        except Exception:
            return None
        return None
    target = str(dev).strip().lower()
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0 and target in d.get("name", "").lower():
                return i
    except Exception:
        pass
    return None


class Speaker:
    """Bounded async pipeline: text queue -> Piper synth worker -> 3-deep audio queue -> playback worker.
    Strictly preserves ordering, minimizes inter-chunk gap (~15ms poll), taps far-end audio for AEC,
    handles Bluetooth device reconnects, and never reports `speaking=True` before playback starts.
    """

    def __init__(self, tts: PiperTTS, on_state=lambda speaking: None, on_error=lambda msg: None, on_status=None, echo_ref=None):
        self.tts, self.on_state, self.on_error = tts, on_state, on_error
        self.on_status = on_status
        self.echo_ref = echo_ref
        self.device = tts.spec.output_device
        self._text_q: queue.Queue = queue.Queue(maxsize=32)
        self._audio_q: queue.Queue = queue.Queue(maxsize=3)
        self._stop = threading.Event()
        self._gen = 0  # bumped by stop(); stale work is discarded
        self._pending = 0  # chunks accepted for current gen and not yet finished playing
        self._seq_counter = 0
        self.chunks_queued = 0
        self.chunks_played = 0
        self.playing_chunk = 0
        self.last_chunk_gap_ms: float | None = None
        self.first_audio_at: float | None = None
        self._last_chunk_ended_at: float | None = None
        self._cv = threading.Condition()
        self._started = False
        self.speaking = False
        self.errors: list[str] = []
        self.last_error: str | None = None

    def start(self):
        with self._cv:
            if not self._started:
                self._started = True
                threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
                threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()

    def status(self) -> dict:
        with self._cv:
            return {
                "speaking": self.speaking,
                "queued": self._pending,
                "playing_chunk": self.playing_chunk,
                "chunks_queued": self.chunks_queued,
                "chunks_played": self.chunks_played,
                "last_chunk_gap_ms": self.last_chunk_gap_ms,
                "last_error": self.last_error,
            }

    def _emit_status(self):
        if self.on_status:
            try:
                self.on_status(self.status())
            except Exception:
                log.exception("speaker status callback")

    def say_chunk(self, raw_or_clean_chunk: str, precleaned: bool = True) -> int:
        """Queue a single natural chunk immediately (for streaming LLM-to-TTS pipeline)."""
        if not self.tts.spec.enabled:
            return 0
        txt = raw_or_clean_chunk.strip() if precleaned else clean_for_speech(raw_or_clean_chunk)
        if not txt:
            return 0
        self.start()
        with self._cv:
            if self._pending == 0 and self.chunks_played >= self.chunks_queued:
                self.chunks_queued = 0
                self.chunks_played = 0
                self.playing_chunk = 0
                self.last_chunk_gap_ms = None
                self.first_audio_at = None
                self._last_chunk_ended_at = None
            gen = self._gen
            self._seq_counter += 1
            seq = self._seq_counter
            self.chunks_queued += 1
            self._pending += 1
        try:
            self._text_q.put((gen, seq, txt), timeout=1.0)
            self._emit_status()
            return 1
        except queue.Full:
            self._done(gen, 1)
            return 0

    def say(self, text: str) -> int:
        """Queue text (cleaned and sentence-packed). Returns number of chunks queued."""
        if not self.tts.spec.enabled:
            return 0
        self.start()
        chunks = split_chunks(clean_for_speech(text), self.tts.spec.chunk_chars)
        if not chunks:
            return 0
        with self._cv:
            if self._pending == 0 and self.chunks_played >= self.chunks_queued:
                self.chunks_queued = 0
                self.chunks_played = 0
                self.playing_chunk = 0
                self.last_chunk_gap_ms = None
                self.first_audio_at = None
                self._last_chunk_ended_at = None
            gen = self._gen
            base_seq = self._seq_counter
            self._seq_counter += len(chunks)
            self.chunks_queued += len(chunks)
            self._pending += len(chunks)
        queued = 0
        for idx, c in enumerate(chunks):
            try:
                self._text_q.put((gen, base_seq + idx + 1, c), timeout=1.0)
                queued += 1
            except queue.Full:
                self._done(gen, len(chunks) - queued)
                break
        if queued:
            self._emit_status()
        return queued

    def _done(self, gen: int | None = None, n: int = 1, played: bool = False):
        with self._cv:
            if gen is None or gen == self._gen:
                self._pending = max(0, self._pending - n)
                if played:
                    self.chunks_played += n
                    self._last_chunk_ended_at = time.monotonic()
                if self._pending == 0:
                    self.playing_chunk = 0
                self._cv.notify_all()
        self._emit_status()

    def _record_error(self, msg: str):
        self.last_error = msg
        self.errors.append(msg)
        del self.errors[:-20]
        try:
            self.on_error(msg)
        except Exception:
            log.exception("speaker error callback")

    def _synth_loop(self):
        while True:
            item = self._text_q.get()
            gen, seq, text = item
            if gen != self._gen:
                continue
            try:
                sr, audio = self.tts.synth(text)
            except Exception as e:  # keep the loop alive; surface the error
                log.exception("tts synth failed")
                self._record_error(f"Piper synthesis failed: {e}")
                self._done(gen)
                continue
            while gen == self._gen:
                try:
                    self._audio_q.put((gen, seq, sr, audio), timeout=0.2)
                    break
                except queue.Full:
                    pass
            else:
                self._done(gen)

    def _play_with_bt_recovery(self, sd, audio: np.ndarray, sr: int):
        """Play audio chunk, recovering once from Bluetooth output reconnects or missing device fallback."""
        dev = _resolve_output_device(sd, self.tts.spec.output_device)
        try:
            sd.play(audio, sr, device=dev)
        except Exception as first_err:
            log.warning("playback error (%s); refreshing audio devices and retrying", first_err)
            try:
                if hasattr(sd, "_terminate") and hasattr(sd, "_initialize"):
                    sd._terminate()
                    sd._initialize()
                retry_dev = _resolve_output_device(sd, self.tts.spec.output_device)
                sd.play(audio, sr, device=retry_dev)
            except Exception:
                # If explicit Bluetooth device disappeared, fall back once to OS default output if available
                if dev is not None:
                    sd.play(audio, sr, device=None)
                else:
                    raise first_err

    def _play_loop(self):
        import sounddevice as sd
        while True:
            gen, seq, sr, audio = self._audio_q.get()
            if gen != self._gen:
                self._done(gen)
                continue
            played_ok = False
            try:
                now = time.monotonic()
                with self._cv:
                    self.playing_chunk += 1
                    if self.first_audio_at is None:
                        self.first_audio_at = now
                    if self._last_chunk_ended_at is not None and self.playing_chunk > 1:
                        self.last_chunk_gap_ms = round((now - self._last_chunk_ended_at) * 1000.0, 1)
                if self.echo_ref is not None:
                    self.echo_ref.push_playback(audio, sr)
                self._play_with_bt_recovery(sd, audio, sr)
                # Transition to SPEAKING ONLY right when playback has started
                self._set(True)
                self._emit_status()
                end = threading.Event()
                while not end.wait(0.015):
                    if gen != self._gen or not sd.get_stream().active:
                        break
                if gen != self._gen:
                    sd.stop()
                else:
                    played_ok = True
            except Exception as e:
                log.exception("playback failed")
                self._record_error(f"Audio playback failed: {e}")
            finally:
                self._done(gen, played=played_ok)
                with self._cv:
                    idle = self._pending == 0
                if idle and self._audio_q.empty() and self._text_q.empty():
                    self._set(False)

    def _set(self, v: bool):
        if v != self.speaking:
            self.speaking = v
            try:
                self.on_state(v)
            except Exception:
                log.exception("speaker callback")
            self._emit_status()

    def stop(self):
        """Stop talking now and discard everything queued."""
        with self._cv:
            self._gen += 1
            self._pending = 0
            self.playing_chunk = 0
            self._cv.notify_all()
        if self.echo_ref is not None:
            self.echo_ref.clear()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        for q in (self._text_q, self._audio_q):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._set(False)
        self._emit_status()

    def wait_idle(self, timeout: float = 60) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)
