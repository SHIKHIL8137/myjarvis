"""Text-to-speech: Piper (CPU, offline). Text cleaning, sentence-packed chunking, disk cache, and a Speaker that
synthesises chunk N+1 while chunk N plays (no gaps), and can be stopped instantly."""
import hashlib
import io
import logging
import queue
import re
import threading
import wave

import numpy as np

from .config import DATA, ROOT, TTSSpec

log = logging.getLogger("jarvis.tts")

EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿️‍]+")
SENT = re.compile(r"(?<=[.!?])\s+")


def clean_for_speech(text: str) -> str:
    """Strip everything a human would not say: code, markdown, emoji, URLs."""
    had_code = "```" in text
    t = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = EMOJI.sub("", t)
    t = re.sub(r"^\s*(#{1,6}|[-*+>]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*_~#|]+", "", t)
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


class PiperTTS:
    def __init__(self, spec: TTSSpec):
        self.spec = spec
        self._voice = None
        self._lock = threading.Lock()
        self.cache_dir = DATA / "tts_cache"

    @property
    def ready(self) -> bool:
        return (ROOT / "models" / "piper" / f"{self.spec.voice}.onnx").exists()

    def load(self):
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice
                self._voice = PiperVoice.load(ROOT / "models" / "piper" / f"{self.spec.voice}.onnx")
                log.info("piper voice loaded: %s", self.spec.voice)
        return self._voice

    def synth(self, text: str) -> tuple[int, np.ndarray]:
        """-> (sample_rate, int16 mono). Cached on disk by text+voice+speed."""
        from piper import SynthesisConfig
        key = hashlib.sha1(f"{self.spec.voice}|{self.spec.speed}|{text}".encode()).hexdigest()
        f = self.cache_dir / f"{key}.wav"
        if f.exists():
            with wave.open(str(f)) as w:
                return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        v = self.load()
        cfg = SynthesisConfig(length_scale=1.0 / self.spec.speed)
        parts, sr = [], 22050
        with self._lock:  # Piper voices are not thread-safe
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


class Speaker:
    """say(text) queues text; a synth thread feeds a 2-deep audio queue; a player thread plays. stop() is immediate."""
    def __init__(self, tts: PiperTTS, on_state=lambda speaking: None):
        self.tts, self.on_state = tts, on_state
        self.device = tts.spec.output_device
        self._text_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._gen = 0  # bumped by stop(); stale work is discarded
        self._pending = 0  # chunks accepted but not yet finished playing
        self._cv = threading.Condition()
        self._started = False
        self.speaking = False
        self.errors: list[str] = []

    def start(self):
        if not self._started:
            self._started = True
            threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
            threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()

    def say(self, text: str) -> int:
        """Queue text (cleaned and chunked). Returns number of chunks queued."""
        if not self.tts.spec.enabled:
            return 0
        self.start()
        chunks = split_chunks(clean_for_speech(text), self.tts.spec.chunk_chars)
        with self._cv:
            self._pending += len(chunks)
        for c in chunks:
            self._text_q.put((self._gen, c))
        return len(chunks)

    def _done(self, n=1):
        with self._cv:
            self._pending = max(0, self._pending - n)
            self._cv.notify_all()

    def _synth_loop(self):
        while True:
            gen, text = self._text_q.get()
            if gen != self._gen:
                continue
            try:
                sr, audio = self.tts.synth(text)
            except Exception as e:  # keep the loop alive; surface the error
                log.exception("tts synth failed")
                self.errors.append(str(e))
                self._done()
                continue
            while gen == self._gen:
                try:
                    self._audio_q.put((gen, sr, audio), timeout=0.2)
                    break
                except queue.Full:
                    pass
            else:
                self._done()

    def _play_loop(self):
        import sounddevice as sd
        while True:
            gen, sr, audio = self._audio_q.get()
            if gen != self._gen:
                self._done()
                continue
            try:
                self._set(True)
                sd.play(audio, sr, device=self.device)
                dur = len(audio) / sr
                end = threading.Event()
                while not end.wait(0.05):
                    if gen != self._gen or not sd.get_stream().active:
                        break
                sd.stop() if gen != self._gen else None
            except Exception as e:
                log.exception("playback failed")
                self.errors.append(str(e))
            finally:
                self._done()
                if self._audio_q.empty() and self._text_q.empty():
                    with self._cv:
                        idle = self._pending == 0
                    if idle:
                        self._set(False)

    def _set(self, v: bool):
        if v != self.speaking:
            self.speaking = v
            try:
                self.on_state(v)
            except Exception:
                log.exception("speaker callback")

    def stop(self):
        """Stop talking now and discard everything queued."""
        self._gen += 1
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
        with self._cv:
            self._pending = 0
            self._cv.notify_all()
        self._set(False)

    def wait_idle(self, timeout: float = 60) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)
