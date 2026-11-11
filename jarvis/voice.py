"""Continuous hands-free voice loop with formal Audio State Machine, Safe Half-Duplex mic suspension,
optional AEC-verified Full-Duplex barge-in, hybrid Silero VAD + adaptive RMS, and Bluetooth recovery."""
import collections
from enum import StrEnum
import logging
import queue
import threading
import time

import numpy as np

from .aec import NLMSEchoCanceller, discover_aec_backends
from .config import Settings
from .stt import SR, Whisper, resample_audio
from .wakeword import FRAME, WakeWord

log = logging.getLogger("jarvis.voice")
_VAD_CHUNK = 512

_BARGE_PRESETS = {
    "high": {"confirm_ms": 160, "vad_thr": 0.45, "rms_mult": 2.2, "min_rms": 260.0},
    "medium": {"confirm_ms": 240, "vad_thr": 0.58, "rms_mult": 2.8, "min_rms": 360.0},
    "low": {"confirm_ms": 320, "vad_thr": 0.72, "rms_mult": 3.5, "min_rms": 500.0},
}


class VoiceState(StrEnum):
    IDLE = "IDLE"
    WAKE_WORD = "WAKE_WORD"
    LISTENING = "LISTENING"
    SPEECH_DETECTED = "SPEECH_DETECTED"
    PROCESSING = "PROCESSING"
    GENERATING = "GENERATING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    RESUMING = "RESUMING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


_LEGACY_STATE = {
    VoiceState.IDLE: "off",
    VoiceState.WAKE_WORD: "listening",
    VoiceState.LISTENING: "listening",
    VoiceState.SPEECH_DETECTED: "recording",
    VoiceState.PROCESSING: "transcribing",
    VoiceState.GENERATING: "thinking",
    VoiceState.SPEAKING: "speaking",
    VoiceState.INTERRUPTED: "recording",
    VoiceState.RESUMING: "listening",
    VoiceState.PAUSED: "paused",
    VoiceState.ERROR: "off",
}


class _StreamingSileroVAD:
    """Lightweight streaming wrapper around faster-whisper's bundled Silero VAD ONNX model."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self._model = get_vad_model()
        self._res = np.zeros(0, dtype=np.float32)

    def reset(self):
        self._res = np.zeros(0, dtype=np.float32)

    def max_prob(self, pcm_i16: np.ndarray) -> float:
        audio = pcm_i16.astype(np.float32) / 32768.0
        buf = np.concatenate([self._res, audio]) if len(self._res) else audio
        n_full = (len(buf) // _VAD_CHUNK) * _VAD_CHUNK
        if n_full == 0:
            self._res = buf
            return 0.0
        self._res = buf[n_full:]
        probs = self._model(buf[:n_full], num_samples=_VAD_CHUNK)
        return float(np.max(probs)) if len(probs) else 0.0


def _resolve_input_device(sd, dev):
    """Resolve input device by int index, name substring, or None (system default)."""
    if dev is None or dev == "":
        return None
    if isinstance(dev, int):
        try:
            info = sd.query_devices(dev, "input")
            if info and info.get("max_input_channels", 0) > 0:
                return dev
        except Exception:
            return None
        return None
    target = str(dev).strip().lower()
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and target in d.get("name", "").lower():
                return i
    except Exception:
        pass
    return None


class VoiceLoop:
    def __init__(
        self,
        settings: Settings,
        wake: WakeWord,
        stt: Whisper,
        speaker,
        emit,
        on_text,
        on_result=None,
        on_utterance=None,
        on_interrupt=None,
        aec: NLMSEchoCanceller | None = None,
        echo_ref=None,
    ):
        """emit(event: dict) publishes UI events; on_utterance() notifies async turn consumer."""
        self.s, self.wake, self.stt, self.speaker, self.emit = settings, wake, stt, speaker, emit
        self.on_text = on_text
        self.on_result = on_result
        self.on_utterance = on_utterance
        self.on_interrupt = on_interrupt
        self.aec = aec if aec is not None else NLMSEchoCanceller()
        self.echo_ref = echo_ref

        self.audio_state: VoiceState = VoiceState.IDLE
        self.state: str = "off"
        self.mode: str = settings.audio.voice_mode
        self.paused: bool = False
        self.mic_suspended: bool = False
        self.last_error: str | None = None
        self.frames_processed = 0

        self._ctl_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._run = threading.Event()
        self._turn_gate = threading.Event()  # set = mic suspended during half-duplex turn
        self._turn_wake = threading.Event()  # wakes paused/suspended loop promptly
        self._ptt_trigger = threading.Event()
        self._one_shot_ptt = False

        # Bounded utterance queue (prevents unbounded audio growth and duplicate utterance processing)
        self.utterance_queue: queue.Queue[tuple[int, np.ndarray]] = queue.Queue(maxsize=2)
        self._utt_seq = 0
        self._active_utt_id = 0
        self._interrupted_utt_id = 0
        self._followup_until = 0.0
        self._barge_seed: list[np.ndarray] = []

        self._vad: _StreamingSileroVAD | None = None
        self._vad_failed = False

    def duplex_info(self) -> dict:
        """Report requested vs effective audio duplex mode, AEC verification, and barge-in eligibility."""
        a = self.s.audio
        backends = discover_aec_backends()
        requested = a.duplex_mode
        aec_verified = bool(self.aec and self.aec.verified)
        if requested == "full_duplex" and not a.suppress_mic_during_tts and a.echo_cancellation != "off":
            if aec_verified:
                return {
                    "requested": requested,
                    "effective": "full_duplex",
                    "fallback": False,
                    "aec_verified": True,
                    "aec_erle_db": round(self.aec.last_erle_db, 1),
                    "barge_in_available": bool(a.barge_in_enabled),
                    "backends": backends,
                    "reason": "Full-duplex active with verified acoustic echo cancellation.",
                }
            return {
                "requested": requested,
                "effective": "half_duplex",
                "fallback": True,
                "aec_verified": False,
                "aec_erle_db": round(self.aec.last_erle_db if self.aec else 0.0, 1),
                "barge_in_available": False,
                "backends": backends,
                "reason": "WebRTC/hardware AEC unavailable or unverified for Bluetooth speaker; fell back to safe half-duplex.",
            }
        return {
            "requested": requested,
            "effective": "half_duplex",
            "fallback": False,
            "aec_verified": aec_verified,
            "aec_erle_db": round(self.aec.last_erle_db if self.aec else 0.0, 1),
            "barge_in_available": False,
            "backends": backends,
            "reason": "Safe Half-Duplex active: microphone suspends during TTS playback to prevent speaker echo feedback.",
        }

    @property
    def _is_half_duplex(self) -> bool:
        return self.duplex_info()["effective"] == "half_duplex"

    # ---- state helpers -------------------------------------------------------------------------------
    def _transition(self, new_state: VoiceState, reason: str = ""):
        with self._state_lock:
            changed = (new_state != self.audio_state)
            self.audio_state = new_state
            self.state = _LEGACY_STATE[new_state]
            if changed or reason:
                d = self.duplex_info()
                self.emit({
                    "type": "voice_state",
                    "state": self.state,
                    "audio_state": self.audio_state.value,
                    "mode": self.mode,
                    "paused": self.paused,
                    "mic_suspended": self.mic_suspended,
                    "duplex_effective": d["effective"],
                    "barge_in_available": d["barge_in_available"],
                    "aec_verified": d["aec_verified"],
                    "reason": reason,
                })

    def _entry_state(self) -> VoiceState:
        if self.mode == "wake_word":
            return VoiceState.WAKE_WORD
        return VoiceState.LISTENING

    def _drain_queue(self):
        while True:
            try:
                self.utterance_queue.get_nowait()
            except queue.Empty:
                break

    # ---- public session controls ---------------------------------------------------------------------
    def set_mode(self, mode: str):
        mode = (mode or "continuous").strip().lower()
        if mode not in ("continuous", "ptt", "wake_word"):
            raise ValueError(f"invalid voice mode: {mode!r}")
        with self._ctl_lock:
            self.mode = mode
            self.s.audio.voice_mode = mode
            self.s.wake.enabled = (mode == "wake_word")
            self._one_shot_ptt = (mode == "ptt")
            if self._thread and self._thread.is_alive() and not self.paused:
                if self.audio_state in (VoiceState.WAKE_WORD, VoiceState.LISTENING):
                    self._transition(self._entry_state(), reason="mode_change")

    def enable(self, mode: str | None = None, *, one_shot_ptt: bool = False):
        with self._ctl_lock:
            if mode is not None:
                self.set_mode(mode)
            else:
                self.mode = self.s.audio.voice_mode
            self.paused = False
            self.mic_suspended = False
            self.last_error = None
            self._turn_gate.clear()
            self._turn_wake.set()
            if self._thread and self._thread.is_alive():
                if one_shot_ptt:
                    self._one_shot_ptt = True
                if self.audio_state == VoiceState.PAUSED:
                    self._transition(self._entry_state(), reason="resumed")
                return
            self._one_shot_ptt = bool(one_shot_ptt or self.mode == "ptt")
            self._drain_queue()
            self._run.set()
            self._thread = threading.Thread(target=self._loop, name="voice-loop", daemon=True)
            self._thread.start()

    def start_session(self, mode: str | None = None):
        self.enable(mode=mode)

    def pause(self):
        with self._ctl_lock:
            if not (self._thread and self._thread.is_alive()):
                return
            self.paused = True
            self._turn_gate.clear()
            self._turn_wake.set()
            self.speaker.stop()
            if self.on_interrupt:
                try:
                    self.on_interrupt()
                except Exception:
                    log.exception("on_interrupt during pause")
            self._transition(VoiceState.PAUSED, reason="user_paused")

    def resume(self):
        with self._ctl_lock:
            if not (self._thread and self._thread.is_alive()):
                self.enable(mode=self.mode)
                return
            self.paused = False
            self.mic_suspended = False
            self._turn_gate.clear()
            if self._vad:
                self._vad.reset()
            self._transition(self._entry_state(), reason="user_resumed")
            self._turn_wake.set()

    def interrupt(self, barge_frames: list[np.ndarray] | None = None, reason: str = "barge_in"):
        """Immediately stop TTS & active generation. Preserves barge-in frames in verified full-duplex."""
        with self._state_lock:
            self._interrupted_utt_id = self._active_utt_id
            self._turn_gate.clear()
            if barge_frames:
                self._barge_seed = [f.copy() for f in barge_frames]
            self._transition(VoiceState.INTERRUPTED, reason=reason)
        self.speaker.stop()
        if self.on_interrupt:
            try:
                self.on_interrupt()
            except Exception:
                log.exception("on_interrupt failed")
        self._turn_wake.set()
        if not barge_frames:
            with self._state_lock:
                if self._run.is_set() and not self.paused and self.audio_state == VoiceState.INTERRUPTED:
                    self._transition(self._entry_state(), reason="interrupt_ready")

    def disable(self, join: bool = True):
        with self._ctl_lock:
            self._run.clear()
            self.paused = False
            self.mic_suspended = False
            self._turn_gate.clear()
            self._turn_wake.set()
            self._ptt_trigger.clear()
            self._one_shot_ptt = False
            self._barge_seed = []
            self._drain_queue()
            th = self._thread
        self.speaker.stop()
        if self.on_interrupt:
            try:
                self.on_interrupt()
            except Exception:
                pass
        if join and th and th is not threading.current_thread() and th.is_alive():
            th.join(timeout=3)
        with self._state_lock:
            if not self._run.is_set():
                self._transition(VoiceState.IDLE, reason="stopped")

    def stop_session(self, join: bool = True):
        self.disable(join=join)

    def push_to_talk(self):
        """Mode A manual capture: opens mic for a single utterance without mutating configured voice_mode."""
        with self._ctl_lock:
            running = bool(self._thread and self._thread.is_alive())
            if not running:
                self.enable(one_shot_ptt=True)
            else:
                if self.paused:
                    self.resume()
            self._ptt_trigger.set()

    def mark_generating(self, utt_id: int):
        """Called by Assistant when Qwen begins generating a response for `utt_id`."""
        with self._state_lock:
            if (
                self._run.is_set()
                and not self.paused
                and utt_id == self._active_utt_id
                and utt_id != self._interrupted_utt_id
                and self.audio_state == VoiceState.PROCESSING
            ):
                self._transition(VoiceState.GENERATING, reason="llm_generating")

    def mark_speaking(self, utt_id: int):
        """Called ONLY when actual TTS audio playback begins."""
        with self._state_lock:
            if (
                self._run.is_set()
                and not self.paused
                and utt_id == self._active_utt_id
                and utt_id != self._interrupted_utt_id
                and self.audio_state in (VoiceState.PROCESSING, VoiceState.GENERATING)
            ):
                self._transition(VoiceState.SPEAKING, reason="tts_started")

    def on_speaker_state(self, is_speaking: bool):
        """Direct hook from Speaker._set(): ensures SPEAKING reflects true audio output start."""
        with self._state_lock:
            uid = self._active_utt_id
        if is_speaking and uid:
            self.mark_speaking(uid)

    def complete_turn(self, utt_id: int, was_interrupted: bool = False):
        """Called ONLY after LLM + Speaker.wait_idle() have genuinely completed."""
        with self._state_lock:
            self._turn_gate.clear()
            if not self._run.is_set():
                self._transition(VoiceState.IDLE, reason="stopped")
                self._turn_wake.set()
                return
            if utt_id != self._active_utt_id:
                self._turn_wake.set()
                return
            if self.paused:
                self._transition(VoiceState.PAUSED, reason="paused")
                self._turn_wake.set()
                return
            if was_interrupted or utt_id == self._interrupted_utt_id or self.audio_state in (VoiceState.SPEECH_DETECTED, VoiceState.INTERRUPTED):
                self._turn_wake.set()
                return

            if self._one_shot_ptt or self.mode == "ptt":
                self._one_shot_ptt = False
                self._run.clear()
                self._transition(VoiceState.IDLE, reason="ptt_complete")
                self._turn_wake.set()
                return

            if self._vad:
                self._vad.reset()
            self._transition(VoiceState.RESUMING, reason="turn_complete")
            self._turn_wake.set()

    # ---- audio internals -----------------------------------------------------------------------------
    def _get_vad(self) -> _StreamingSileroVAD | None:
        if not self.s.audio.use_silero_vad or self._vad_failed:
            return None
        if self._vad is None:
            try:
                self._vad = _StreamingSileroVAD()
            except Exception as e:
                log.warning("Silero VAD unavailable in voice loop, falling back to RMS: %s", e)
                self._vad_failed = True
                return None
        return self._vad

    def _open_stream_once(self, sd):
        a = self.s.audio
        dev_arg = _resolve_input_device(sd, a.input_device)
        req_sr = int(a.sample_rate or SR)
        try:
            block = int(round(FRAME * req_sr / SR))
            return sd.InputStream(samplerate=req_sr, channels=1, dtype="int16", blocksize=block, device=dev_arg), req_sr, block
        except Exception as first_err:
            try:
                dev = sd.query_devices(dev_arg, "input") if dev_arg is not None else sd.query_devices(kind="input")
                nat_sr = int(dev["default_samplerate"])
                if nat_sr > 0 and nat_sr != req_sr:
                    block = int(round(FRAME * nat_sr / SR))
                    return sd.InputStream(samplerate=nat_sr, channels=1, dtype="int16", blocksize=block, device=dev_arg), nat_sr, block
            except Exception:
                pass
            raise first_err

    def _open_stream(self, sd):
        try:
            return self._open_stream_once(sd)
        except Exception as first_err:
            # Refresh PortAudio table only after an active session (e.g., BT/USB reconnect between turns)
            if self.frames_processed > 0 and hasattr(sd, "_terminate") and hasattr(sd, "_initialize"):
                try:
                    sd._terminate()
                    sd._initialize()
                    return self._open_stream_once(sd)
                except Exception:
                    pass
            raise first_err

    def _submit_utterance(self, audio_i16: np.ndarray) -> bool:
        """Queue captured speech and signal whether half-duplex should suspend mic immediately."""
        min_samples = int(SR * (self.s.audio.min_utterance_ms / 1000.0))
        if len(audio_i16) < min_samples:
            self._transition(self._entry_state(), reason="too_short")
            return False
        audio_f32 = audio_i16.astype(np.float32) / 32768.0
        suspend_mic = self._is_half_duplex
        with self._state_lock:
            self._utt_seq += 1
            utt_id = self._utt_seq
            self._active_utt_id = utt_id
            if suspend_mic:
                self._turn_gate.set()
                self._turn_wake.clear()
                self.mic_suspended = True
            self._transition(VoiceState.PROCESSING, reason="utterance_ready")

        if self.on_utterance is not None:
            while True:
                try:
                    self.utterance_queue.put_nowait((utt_id, audio_f32))
                    break
                except queue.Full:
                    try:
                        self.utterance_queue.get_nowait()
                    except queue.Empty:
                        break
            self.on_utterance()
        else:
            self._process_standalone(utt_id, audio_f32)
        return suspend_mic

    def _process_standalone(self, utt_id: int, audio_f32: np.ndarray):
        try:
            r = self.stt.transcribe(audio_f32)
        except Exception as e:
            log.exception("transcribe")
            self.emit({"type": "error", "message": f"transcription failed: {e}"})
            self.complete_turn(utt_id)
            return
        text = r["text"].strip()
        self.emit({"type": "transcript", **r, "text": text})
        if text:
            if self.on_result is not None:
                self.on_result(r)
            else:
                self.on_text(text)
        self.complete_turn(utt_id)

    def _sleep_interruptible(self, seconds: float):
        deadline = time.monotonic() + max(0.0, seconds)
        while self._run.is_set() and not self.paused and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            self._turn_wake.clear()
            self._turn_wake.wait(timeout=min(0.05, remaining))

    # ---- main audio loop -----------------------------------------------------------------------------
    def _loop(self):
        import sounddevice as sd
        vad = self._get_vad()
        if self.mode == "wake_word" or self.s.wake.enabled:
            try:
                self.wake.load()
            except Exception as e:
                if self.mode == "wake_word":
                    self.last_error = f"wake word model unavailable: {e}"
                    self.emit({"type": "error", "message": self.last_error})
                    self._transition(VoiceState.ERROR, reason="wake_load_failed")
                    return
                log.warning("wake model preload skipped: %s", e)

        wake_followup_after_turn = False
        try:
            while self._run.is_set():
                # Suspended during half-duplex TTS or user PAUSE: input stream is CLOSED & released
                if self.paused:
                    self.mic_suspended = True
                    self._turn_wake.wait(timeout=0.05)
                    self._turn_wake.clear()
                    continue

                if self._turn_gate.is_set() and self._is_half_duplex:
                    self.mic_suspended = True
                    self._turn_wake.wait(timeout=0.05)
                    self._turn_wake.clear()
                    continue

                # Transitioning back from half-duplex turn: apply room acoustic settle delay BEFORE reopening mic
                if self.audio_state == VoiceState.RESUMING:
                    delay_s = float(getattr(self.s.audio, "mic_resume_delay_ms", self.s.audio.post_tts_cooldown_ms) or 0) / 1000.0
                    if delay_s > 0:
                        self._sleep_interruptible(delay_s)
                    if not self._run.is_set() or self.paused:
                        continue
                    wake_followup_after_turn = True

                try:
                    stream, cap_sr, blocksize = self._open_stream(sd)
                except Exception as e:
                    self.last_error = f"cannot open microphone: {e}"
                    log.error(self.last_error)
                    self.emit({"type": "error", "message": self.last_error})
                    self._transition(VoiceState.ERROR, reason="open_failed")
                    return

                self.last_error = None
                self.mic_suspended = False
                a = self.s.audio
                noise = float(a.noise_gate_rms) * 0.8
                pre_frames = max(1, int(round(a.pre_roll_ms / 80)))
                pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=pre_frames)
                pre_snap: list[np.ndarray] = []
                onset_buf: list[np.ndarray] = []
                rec_buf: list[np.ndarray] = []
                barge_buf: list[np.ndarray] = []
                speech_run_ms = 0.0
                silence_run_ms = 0.0
                rec_samples = 0
                listen_idle_ms = 0.0
                barge_run_ms = 0.0
                empty_reads = 0
                has_voiced_in_rec = True

                with stream:
                    # Enter active listening (with follow-up window if returning from wake-word turn)
                    if wake_followup_after_turn and self.mode == "wake_word":
                        wake_followup_after_turn = False
                        fw = float(a.wake_followup_s or 0.0)
                        if fw > 0:
                            self._followup_until = time.time() + fw
                            self._transition(VoiceState.LISTENING, reason="wake_followup")
                        else:
                            self._transition(VoiceState.WAKE_WORD, reason="turn_complete")
                    else:
                        self._transition(self._entry_state(), reason="stream_opened")

                    while self._run.is_set() and not self.paused and not (self._turn_gate.is_set() and self._is_half_duplex):
                        a = self.s.audio
                        if pre_roll.maxlen != max(1, int(round(a.pre_roll_ms / 80))):
                            pre_roll = collections.deque(pre_roll, maxlen=max(1, int(round(a.pre_roll_ms / 80))))

                        try:
                            frame, _ = stream.read(blocksize)
                        except Exception as read_err:
                            raise OSError(f"microphone disconnected or read failed: {read_err}") from read_err

                        raw_mono = frame[:, 0] if frame.ndim > 1 else frame
                        if len(raw_mono) < max(1, blocksize // 2):
                            empty_reads += 1
                            if empty_reads > 50:
                                raise OSError("microphone stopped delivering audio frames")
                            continue
                        empty_reads = 0

                        if cap_sr != SR:
                            f32 = resample_audio(raw_mono.astype(np.float32) / 32768.0, cap_sr, SR, remove_dc=False)
                            frame16 = (np.clip(f32, -1.0, 1.0) * 32767.0).astype(np.int16)
                        else:
                            frame16 = raw_mono
                        if len(frame16) < FRAME and cap_sr == SR:
                            continue

                        self.frames_processed += 1
                        frame_ms = (len(frame16) / SR) * 1000.0
                        rms = float(np.sqrt(np.mean(frame16.astype(np.float32) ** 2)))
                        now = time.time()

                        with self._state_lock:
                            if self.audio_state == VoiceState.INTERRUPTED and self._barge_seed:
                                rec_buf = list(self._barge_seed)
                                self._barge_seed.clear()
                                rec_samples = sum(len(x) for x in rec_buf)
                                silence_run_ms = 0.0
                                has_voiced_in_rec = True
                                barge_buf.clear()
                                barge_run_ms = 0.0
                                self._transition(VoiceState.SPEECH_DETECTED, reason="barge_in_recording")
                            st = self.audio_state

                        # Returning from full-duplex turn in same stream
                        if st == VoiceState.RESUMING:
                            delay_s = float(getattr(a, "mic_resume_delay_ms", a.post_tts_cooldown_ms) or 0) / 1000.0
                            if delay_s > 0:
                                self._sleep_interruptible(delay_s)
                            pre_roll.clear()
                            if self.mode == "wake_word":
                                fw = float(a.wake_followup_s or 0.0)
                                if fw > 0:
                                    self._followup_until = time.time() + fw
                                    self._transition(VoiceState.LISTENING, reason="wake_followup")
                                else:
                                    self._transition(VoiceState.WAKE_WORD, reason="turn_complete")
                            else:
                                self._transition(VoiceState.LISTENING, reason="turn_complete")
                            continue

                        # Full-duplex ONLY (verified AEC): monitor verified barge-in during GENERATING / SPEAKING
                        if st in (VoiceState.PROCESSING, VoiceState.GENERATING, VoiceState.SPEAKING):
                            if self.speaker.speaking and st != VoiceState.SPEAKING:
                                self._transition(VoiceState.SPEAKING, reason="speaker_active")
                                st = VoiceState.SPEAKING
                            if st != VoiceState.SPEAKING or not self.duplex_info()["barge_in_available"]:
                                if vad:
                                    vad.reset()
                                barge_buf.clear()
                                barge_run_ms = 0.0
                                continue

                            # Run NLMS AEC subtraction using far-end TTS reference when playback is active
                            proc_frame16 = frame16
                            if self.echo_ref is not None and self.aec is not None and self.echo_ref.is_playback_recent():
                                far_ref = self.echo_ref.snapshot(len(frame16) + self.aec.delay_samples)
                                cleaned, _ = self.aec.process_block(frame16, far_ref[: len(frame16)])
                                proc_frame16 = (np.clip(cleaned, -1.0, 1.0) * 32767.0).astype(np.int16)

                            preset = _BARGE_PRESETS.get(a.barge_in_sensitivity, _BARGE_PRESETS["medium"])
                            b_rms = float(np.sqrt(np.mean(proc_frame16.astype(np.float32) ** 2)))
                            vad_prob = vad.max_prob(proc_frame16) if vad else 0.0
                            rms_hit = b_rms > max(preset["min_rms"], preset["rms_mult"] * noise)
                            vad_hit = vad is not None and vad_prob >= preset["vad_thr"] and b_rms > max(120.0, 1.5 * noise)
                            wake_hit = False
                            if self.s.wake.enabled and self.wake.ready:
                                wake_hit = self.wake.score(proc_frame16[:FRAME]) >= min(self.s.wake.threshold + 0.25, 0.99)
                            if vad_hit or rms_hit or wake_hit:
                                barge_buf.append(proc_frame16.copy())
                                barge_run_ms += frame_ms
                                if barge_run_ms >= preset["confirm_ms"] or wake_hit:
                                    seed = list(pre_roll)[-1:] + barge_buf if not wake_hit else []
                                    barge_buf.clear()
                                    barge_run_ms = 0.0
                                    self.interrupt(barge_frames=seed, reason="user_barge_in")
                            else:
                                barge_buf.clear()
                                barge_run_ms = 0.0
                            continue

                        if st == VoiceState.WAKE_WORD:
                            noise = 0.97 * noise + 0.03 * min(rms, 2000.0)
                            pre_roll.append(frame16.copy())
                            ptt_hit = self._ptt_trigger.is_set()
                            wake_hit = self.wake.score(frame16[:FRAME]) >= min(self.s.wake.threshold, 0.99) if not ptt_hit else False
                            if ptt_hit or wake_hit:
                                self._ptt_trigger.clear()
                                self.wake.reset()
                                if vad:
                                    vad.reset()
                                onset_buf.clear()
                                rec_buf.clear()
                                speech_run_ms = silence_run_ms = listen_idle_ms = 0.0
                                if ptt_hit:
                                    rec_buf = list(pre_roll)
                                    rec_samples = sum(len(x) for x in rec_buf)
                                    has_voiced_in_rec = False
                                    self._transition(VoiceState.SPEECH_DETECTED, reason="ptt")
                                else:
                                    pre_roll.clear()
                                    self._followup_until = now + max(3.0, float(a.no_speech_timeout_s))
                                    self._transition(VoiceState.LISTENING, reason="wake_detected")
                            continue

                        if st == VoiceState.LISTENING:
                            if self._ptt_trigger.is_set():
                                self._ptt_trigger.clear()
                                rec_buf = list(pre_roll)
                                rec_samples = sum(len(x) for x in rec_buf)
                                onset_buf.clear()
                                pre_roll.clear()
                                speech_run_ms = silence_run_ms = listen_idle_ms = 0.0
                                has_voiced_in_rec = False
                                self._transition(VoiceState.SPEECH_DETECTED, reason="ptt")
                                continue

                            if self.mode == "wake_word" and self._followup_until > 0 and now > self._followup_until and not onset_buf:
                                self._followup_until = 0.0
                                self.wake.reset()
                                self._transition(VoiceState.WAKE_WORD, reason="followup_timeout")
                                continue

                            vad_prob = vad.max_prob(frame16) if vad else 0.0
                            rms_thr = max(float(a.noise_gate_rms), 3.0 * noise)
                            if vad is not None:
                                is_speech = (vad_prob >= a.vad_speech_threshold and rms > max(80.0, 1.3 * noise)) or (rms > max(rms_thr * 1.5, 4.5 * noise))
                            else:
                                is_speech = rms > rms_thr

                            if is_speech:
                                if not onset_buf:
                                    pre_snap = list(pre_roll)
                                onset_buf.append(frame16.copy())
                                speech_run_ms += frame_ms
                                listen_idle_ms = 0.0
                                if speech_run_ms >= a.speech_confirm_ms:
                                    rec_buf = pre_snap + onset_buf
                                    rec_samples = sum(len(x) for x in rec_buf)
                                    pre_snap = []
                                    onset_buf.clear()
                                    pre_roll.clear()
                                    speech_run_ms = 0.0
                                    silence_run_ms = 0.0
                                    has_voiced_in_rec = True
                                    self._followup_until = 0.0
                                    self._transition(VoiceState.SPEECH_DETECTED, reason="speech_confirmed")
                            else:
                                if onset_buf:
                                    for f in onset_buf:
                                        pre_roll.append(f)
                                    onset_buf.clear()
                                    pre_snap = []
                                    speech_run_ms = 0.0
                                pre_roll.append(frame16.copy())
                                noise = 0.97 * noise + 0.03 * min(rms, 2000.0)
                                listen_idle_ms += frame_ms
                                if (self._one_shot_ptt or self.mode == "ptt") and listen_idle_ms >= a.no_speech_timeout_s * 1000.0:
                                    self._one_shot_ptt = False
                                    self._run.clear()
                                    self._transition(VoiceState.IDLE, reason="ptt_timeout")
                            continue

                        if st == VoiceState.SPEECH_DETECTED:
                            rec_buf.append(frame16.copy())
                            rec_samples += len(frame16)
                            if (rec_samples // FRAME) % 2 == 0:
                                self.emit({"type": "level", "v": round(min(1.0, rms / 4000.0), 3)})

                            vad_prob = vad.max_prob(frame16) if vad else 0.0
                            rms_thr = max(float(a.noise_gate_rms), 2.8 * noise)
                            if vad is not None:
                                is_speech = (vad_prob >= a.vad_speech_threshold and rms > max(80.0, 1.2 * noise)) or (rms > max(rms_thr * 1.4, 4.2 * noise))
                            else:
                                is_speech = rms > rms_thr

                            if is_speech:
                                has_voiced_in_rec = True
                                silence_run_ms = 0.0
                            else:
                                silence_run_ms += frame_ms

                            utt_dur_s = rec_samples / SR
                            if not has_voiced_in_rec:
                                if silence_run_ms >= a.no_speech_timeout_s * 1000.0:
                                    rec_buf.clear()
                                    rec_samples = 0
                                    silence_run_ms = 0.0
                                    if vad:
                                        vad.reset()
                                    if self._one_shot_ptt or self.mode == "ptt":
                                        self._run.clear()
                                        self._transition(VoiceState.IDLE, reason="ptt_timeout")
                                    else:
                                        self._transition(self._entry_state(), reason="ptt_timeout")
                            elif silence_run_ms >= a.silence_ms or utt_dur_s >= a.max_utterance_s:
                                payload = np.concatenate(rec_buf)
                                rec_buf.clear()
                                rec_samples = 0
                                silence_run_ms = 0.0
                                if vad:
                                    vad.reset()
                                if self._submit_utterance(payload):
                                    # Half-duplex: exit `with stream:` immediately to release hardware mic
                                    break
                            continue
        except Exception as e:
            self.last_error = f"voice loop stopped: {e}"
            log.exception("voice loop")
            self.emit({"type": "error", "message": self.last_error})
            self._transition(VoiceState.ERROR, reason="loop_error")
        finally:
            self.mic_suspended = False
            if self.audio_state != VoiceState.ERROR:
                self._transition(VoiceState.IDLE, reason="stream_closed")
