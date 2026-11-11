"""Hands-free voice loop (server side, sounddevice): wake word -> record until silence -> Whisper -> callback.
The microphone stream only exists while enabled and is always closed on exit. While Jarvis speaks, the wake word
still works (barge-in) but with a higher threshold so his own voice is less likely to trigger it. Use headphones."""
import logging
import threading
import time

import numpy as np

from .config import Settings
from .stt import SR, Whisper
from .wakeword import FRAME, WakeWord

log = logging.getLogger("jarvis.voice")


class VoiceLoop:
    def __init__(self, settings: Settings, wake: WakeWord, stt: Whisper, speaker, emit, on_text):
        """emit(event: dict) publishes UI events; on_text(text: str) is called from the voice thread with each command."""
        self.s, self.wake, self.stt, self.speaker, self.emit, self.on_text = settings, wake, stt, speaker, emit, on_text
        self.state = "off"  # off | listening | recording | transcribing
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._run = threading.Event()
        self._ptt = threading.Event()
        self.frames_processed = 0

    # ---- control -------------------------------------------------------------------------------------
    def enable(self):
        if self._thread and self._thread.is_alive():
            return
        self._run.set()
        self._thread = threading.Thread(target=self._loop, name="voice-loop", daemon=True)
        self._thread.start()

    def disable(self, join: bool = True):
        self._run.clear()
        if join and self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def push_to_talk(self):
        """Skip the wake word: start recording a command now."""
        self.enable()
        self._ptt.set()

    def _set(self, st: str):
        if st != self.state:
            self.state = st
            self.emit({"type": "voice_state", "state": st})

    # ---- loop ----------------------------------------------------------------------------------------
    def _loop(self):
        import sounddevice as sd
        a = self.s.audio
        try:
            stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=FRAME, device=a.input_device)
        except Exception as e:
            self.last_error = f"cannot open microphone: {e}"
            log.error(self.last_error)
            self.emit({"type": "error", "message": self.last_error})
            self._set("off")
            return
        self.last_error = None
        noise, buf, speech_started, last_speech, t_start = 200.0, [], False, 0.0, 0.0
        recording = False
        try:
            with stream:
                self._set("listening")
                if self.s.wake.enabled:
                    self.wake.load()
                while self._run.is_set():
                    frame, _ = stream.read(FRAME)
                    frame = frame[:, 0]
                    if len(frame) < FRAME:  # short read: skip rather than feed the models a partial frame
                        continue
                    self.frames_processed += 1
                    rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                    now = time.time()
                    if not recording:
                        noise = 0.98 * noise + 0.02 * min(rms, 2000)
                        hit = self._ptt.is_set()
                        if not hit and self.s.wake.enabled:
                            thr = self.s.wake.threshold + (0.25 if self.speaker.speaking else 0.0)
                            hit = self.wake.score(frame) >= min(thr, 0.99)
                        if hit:
                            self._ptt.clear()
                            if self.speaker.speaking:
                                self.speaker.stop()
                            self.wake.reset()
                            recording, buf, speech_started, t_start = True, [], False, now
                            self._set("recording")
                        continue
                    buf.append(frame.copy())
                    if len(buf) % 2 == 0:  # ~6 real microphone levels per second for the UI visualiser
                        self.emit({"type": "level", "v": round(min(1.0, rms / 4000.0), 3)})
                    thr = max(350.0, 3.0 * noise)
                    if rms > thr:
                        speech_started, last_speech = True, now
                    done = (speech_started and now - last_speech > a.silence_ms / 1000) \
                        or (not speech_started and now - t_start > a.no_speech_timeout_s) \
                        or now - t_start > a.max_utterance_s
                    if done:
                        recording = False
                        if speech_started:
                            self._finish(np.concatenate(buf))
                        self._set("listening")
        except Exception as e:
            self.last_error = f"voice loop stopped: {e}"
            log.exception("voice loop")
            self.emit({"type": "error", "message": self.last_error})
        finally:  # stream closed by the with-block: microphone released
            self._set("off")

    def _finish(self, audio_i16: np.ndarray):
        if len(audio_i16) < SR * 0.3:
            return
        self._set("transcribing")
        try:
            r = self.stt.transcribe(audio_i16.astype(np.float32) / 32768.0)
        except Exception as e:
            log.exception("transcribe")
            self.emit({"type": "error", "message": f"transcription failed: {e}"})
            return
        text = r["text"].strip()
        self.emit({"type": "transcript", "text": text, "seconds": r["seconds"]})
        if text:
            self.on_text(text)
