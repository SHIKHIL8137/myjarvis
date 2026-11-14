"""Audio capture validation, diagnostic reporting, and configurable preprocessing profiles.
Always preserves raw input arrays; avoids aggressive denoising or amplifying pure silence into hallucinations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import io
import math
import wave

import numpy as np
from scipy.signal import butter, resample_poly, sosfiltfilt

TARGET_SR = 16000
PREPROCESS_PROFILES = ("off", "light", "standard")
SUPPORTED_INPUT_RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000)


@dataclass
class AudioDiagnosticReport:
    sample_rate: int
    channels: int
    duration_s: float
    num_samples: int
    peak_amplitude: float
    rms_level: float
    clipping_percent: float
    estimated_noise_level: float
    snr_db: float
    silence_percent: float
    vad_speech_duration_s: float
    empty_or_too_short: bool
    clipped: bool
    low_volume: bool
    profile: str = "light"
    gain_applied_db: float = 0.0
    trimmed: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def downmix_to_mono(audio: np.ndarray) -> tuple[np.ndarray, int]:
    """Convert mono or multi-channel array to mono float32 in [-1, 1], returning (mono, original_channels)."""
    raw = np.asarray(audio)
    if raw.size == 0:
        return np.zeros(0, dtype=np.float32), 1
    if np.issubdtype(raw.dtype, np.integer):
        arr = raw.astype(np.float32) / 32768.0
    else:
        arr = raw.astype(np.float32)
    if arr.ndim == 1:
        return np.clip(arr, -1.0, 1.0).astype(np.float32), 1
    # Support both (N, C) from sounddevice/wave and flattened/transposed shapes defensively
    if arr.shape[0] < arr.shape[1] and arr.shape[0] <= 8:
        channels = arr.shape[0]
        mono = arr.mean(axis=0)
    else:
        channels = arr.shape[1]
        mono = arr.mean(axis=1)
    return np.clip(mono, -1.0, 1.0).astype(np.float32), int(channels)


def parse_wav_raw(data: bytes) -> tuple[np.ndarray, int, int]:
    """Parse PCM16 WAV bytes WITHOUT resampling or normalization. Returns (audio_f32_or_2d, sample_rate, channels)."""
    if not data:
        raise ValueError("empty WAV payload")
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch = w.getnchannels()
            sw = w.getsampwidth()
            sr = w.getframerate()
            n = w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a valid WAV file: {e}") from e
    if sw != 2:
        raise ValueError("WAV must be 16-bit PCM")
    if ch < 1 or sr <= 0:
        raise ValueError(f"invalid WAV header: channels={ch}, sample_rate={sr}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        usable = (len(a) // ch) * ch
        a = a[:usable].reshape(-1, ch)
    return a.astype(np.float32), int(sr), int(ch)


def bandlimited_resample(
    audio: np.ndarray,
    src_sr: int,
    dst_sr: int = TARGET_SR,
    remove_dc: bool = False,
) -> np.ndarray:
    """Anti-aliased polyphase resampling on mono float32 audio."""
    if len(audio) == 0:
        return np.zeros(0, dtype=np.float32)
    a = np.asarray(audio, dtype=np.float32)
    if int(src_sr) != int(dst_sr):
        g = math.gcd(int(dst_sr), int(src_sr))
        up, down = int(dst_sr) // g, int(src_sr) // g
        a = resample_poly(a, up, down).astype(np.float32)
    if remove_dc and len(a) > 16:
        a = a - float(np.mean(a))
    return np.clip(a, -1.0, 1.0).astype(np.float32)


def _highpass(audio: np.ndarray, sr: int, cutoff_hz: float) -> np.ndarray:
    """Zero-phase 2nd-order Butterworth high-pass filter for subsonic rumble/DC removal."""
    if len(audio) < 32 or sr <= 0 or cutoff_hz <= 0 or cutoff_hz >= sr * 0.45:
        return audio
    sos = butter(2, cutoff_hz / (0.5 * sr), btype="highpass", output="sos")
    try:
        out = sosfiltfilt(sos, audio).astype(np.float32)
    except ValueError:
        return audio
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def _light_spectral_denoise(audio: np.ndarray, sr: int, noise_floor_rms: float) -> np.ndarray:
    """Conservative gentle spectral gate (max ~10 dB attenuation) preserving speech consonants."""
    if len(audio) < 512 or noise_floor_rms <= 1e-5:
        return audio
    n_fft = 512
    hop = 160
    win = np.hanning(n_fft).astype(np.float32)
    # Pad cleanly to integer frames
    pad_len = (n_fft - (len(audio) % hop)) % hop + n_fft
    padded = np.pad(audio, (0, pad_len), mode="constant")
    frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft)[::hop]
    windowed = frames * win[None, :]
    spec = np.fft.rfft(windowed, axis=1)
    mag = np.abs(spec)
    frame_rms = np.sqrt(np.mean(windowed ** 2, axis=1) + 1e-12)
    quiet_mask = frame_rms <= max(noise_floor_rms * 1.8, float(np.percentile(frame_rms, 20)))
    if not np.any(quiet_mask):
        return audio
    noise_mag = np.mean(mag[quiet_mask], axis=0, keepdims=True)
    # Conservative Wiener-like gain with 0.35 floor so vowels/fricatives never sound metallic
    gain = np.maximum(0.35, 1.0 - 0.85 * (noise_mag / np.maximum(mag, 1e-8)))
    cleaned_frames = np.fft.irfft(spec * gain, n=n_fft, axis=1) * win[None, :]

    out = np.zeros(len(padded), dtype=np.float32)
    norm = np.zeros(len(padded), dtype=np.float32)
    win2 = win ** 2
    for idx, cf in enumerate(cleaned_frames):
        s = idx * hop
        out[s : s + n_fft] += cf
        norm[s : s + n_fft] += win2
    out = (out / np.maximum(norm, np.float32(1e-8))).astype(np.float32)
    return np.clip(out[: len(audio)], -1.0, 1.0).astype(np.float32)


def _frame_rms_stats(mono: np.ndarray, sr: int) -> tuple[float, float, float, float, float]:
    """Compute peak, rms, clipping%, noise_rms (p10 of 30ms frames), silence%."""
    if len(mono) == 0 or sr <= 0:
        return 0.0, 0.0, 0.0, 0.0, 100.0
    peak = float(np.max(np.abs(mono)))
    rms = float(np.sqrt(np.mean(mono ** 2)))
    clip_pct = float(np.mean(np.abs(mono) >= 0.985)) * 100.0

    win = max(64, int(sr * 0.03))
    usable = (len(mono) // win) * win
    if usable >= win:
        wrms = np.sqrt(np.mean(mono[:usable].reshape(-1, win) ** 2, axis=1))
        noise_est = float(np.percentile(wrms, 15))
        speech_thr = max(0.005, 2.2 * noise_est)
        silence_pct = float(np.mean(wrms < speech_thr)) * 100.0
    else:
        noise_est = min(rms, 0.002)
        silence_pct = 100.0 if rms < 0.005 else 0.0
    return peak, rms, clip_pct, noise_est, silence_pct


def _estimate_vad_speech_seconds(mono_16k: np.ndarray, vad_fn=None) -> float:
    """Estimate active speech duration using Silero VAD when available, else short-time energy fallback."""
    if len(mono_16k) < 512:
        return 0.0
    if vad_fn is not None:
        try:
            probs = vad_fn(mono_16k)
            if len(probs) > 0:
                speech_chunks = sum(1 for p in probs if float(p) >= 0.35)
                return round((speech_chunks * 512) / float(TARGET_SR), 3)
        except Exception:
            pass
    # Fallback frame energy mask (30ms windows above noise floor)
    win = int(0.03 * TARGET_SR)
    usable = (len(mono_16k) // win) * win
    if usable < win:
        return 0.0
    wrms = np.sqrt(np.mean(mono_16k[:usable].reshape(-1, win) ** 2, axis=1))
    noise = float(np.percentile(wrms, 15))
    thr = max(0.006, 2.5 * noise)
    active = int(np.sum(wrms >= thr))
    return round((active * win) / float(TARGET_SR), 3)


def _trim_silence_with_margin(mono: np.ndarray, sr: int, noise_est: float, margin_ms: int = 160) -> tuple[np.ndarray, bool]:
    """Trim only excess leading/trailing silence while keeping a generous acoustic margin."""
    if len(mono) <= int(sr * 0.6):
        return mono, False
    win = max(64, int(sr * 0.02))
    usable = (len(mono) // win) * win
    if usable < win * 4:
        return mono, False
    wrms = np.sqrt(np.mean(mono[:usable].reshape(-1, win) ** 2, axis=1))
    thr = max(0.005, 2.2 * noise_est)
    idx = np.where(wrms >= thr)[0]
    if len(idx) == 0:
        return mono, False
    margin_samples = int(sr * (margin_ms / 1000.0))
    start = max(0, int(idx[0]) * win - margin_samples)
    end = min(len(mono), (int(idx[-1]) + 1) * win + margin_samples)
    if start == 0 and end >= len(mono) - margin_samples // 2:
        return mono, False
    if end - start < int(sr * 0.2):
        return mono, False
    return mono[start:end].copy(), True


def diagnose_audio(
    audio: np.ndarray,
    sr: int = TARGET_SR,
    *,
    channels: int = 1,
    profile: str = "light",
    vad_fn=None,
) -> AudioDiagnosticReport:
    """Produce a structured audio diagnostic report from un-altered or mono audio."""
    mono, detected_ch = downmix_to_mono(audio)
    orig_ch = channels if channels > 1 else detected_ch
    n = len(mono)
    dur = round(n / float(max(1, sr)), 3)
    peak, rms, clip_pct, noise_est, silence_pct = _frame_rms_stats(mono, sr)
    snr_db = round(20.0 * math.log10(max(rms, 1e-6) / max(noise_est, 1e-6)), 2) if n > 0 else 0.0
    mono_16k = bandlimited_resample(mono, sr, TARGET_SR) if n > 0 else mono
    vad_dur = _estimate_vad_speech_seconds(mono_16k, vad_fn=vad_fn)

    empty_short = (n == 0) or (dur < 0.12)
    clipped = clip_pct >= 0.5 or peak >= 0.995
    low_vol = (not empty_short) and (rms < 0.006 or peak < 0.03)

    warnings: list[str] = []
    if n == 0:
        warnings.append("empty_recording")
    elif empty_short:
        warnings.append("extremely_short_recording")
    if clipped:
        warnings.append(f"clipping_detected:{clip_pct:.2f}%")
    if low_vol:
        warnings.append("very_low_input_volume")
    if not empty_short and silence_pct >= 92.0 and rms < 0.003:
        warnings.append("mostly_silence")

    return AudioDiagnosticReport(
        sample_rate=int(sr),
        channels=int(orig_ch),
        duration_s=dur,
        num_samples=int(n),
        peak_amplitude=round(peak, 4),
        rms_level=round(rms, 5),
        clipping_percent=round(clip_pct, 3),
        estimated_noise_level=round(noise_est, 5),
        snr_db=snr_db,
        silence_percent=round(silence_pct, 1),
        vad_speech_duration_s=vad_dur,
        empty_or_too_short=empty_short,
        clipped=clipped,
        low_volume=low_vol,
        profile=profile,
        warnings=warnings,
    )


def preprocess_audio(
    audio: np.ndarray,
    src_sr: int = TARGET_SR,
    *,
    dst_sr: int = TARGET_SR,
    channels: int = 1,
    profile: str = "light",
    vad_fn=None,
    pad_short_ms: int = 0,
) -> tuple[np.ndarray, AudioDiagnosticReport]:
    """Safely preprocess captured audio without mutating the input array.
    Profiles:
      - off: stereo-to-mono + bandlimited resampling only
      - light (default): DC removal, bandlimited resampling, gentle 65Hz HPF, bounded gain normalization, margin-safe silence trim
      - standard: light + 85Hz HPF + conservative light noise attenuation
    """
    prof = (profile or "light").strip().lower()
    if prof not in PREPROCESS_PROFILES:
        prof = "light"

    raw_mono, detected_ch = downmix_to_mono(audio)
    diag = diagnose_audio(audio, src_sr, channels=max(channels, detected_ch), profile=prof, vad_fn=vad_fn)
    if len(raw_mono) == 0:
        return np.zeros(0, dtype=np.float32), diag

    a = bandlimited_resample(raw_mono, src_sr, dst_sr)
    if prof == "off":
        return np.clip(a, -1.0, 1.0).astype(np.float32), diag

    if len(a) > 16:
        a = a - float(np.mean(a))

    cutoff = 85.0 if prof == "standard" else 65.0
    a = _highpass(a, dst_sr, cutoff)

    _, _, _, noise_est_16k, _ = _frame_rms_stats(a, dst_sr)
    if prof == "standard" and diag.snr_db < 24.0 and diag.peak_amplitude >= 0.01:
        a = _light_spectral_denoise(a, dst_sr, noise_est_16k)

    # Margin-preserving edge silence trim (never trims short utterances < 0.6s or chops speech)
    a, trimmed = _trim_silence_with_margin(a, dst_sr, noise_est_16k, margin_ms=160)
    diag.trimmed = trimmed

    # Bounded safe amplitude normalization: NEVER boost pure silence / quantization noise
    peak = float(np.max(np.abs(a))) if len(a) else 0.0
    rms = float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
    if peak >= 0.012 and rms >= 0.002 and not diag.empty_or_too_short:
        # Target healthy -3.5 dBFS peak ceiling / ~0.07 RMS, capped at +14 dB (~5.0x)
        max_gain = 5.0
        target_gain = min(max_gain, 0.65 / max(peak, 1e-6), 0.09 / max(rms, 1e-6))
        if target_gain > 1.08:
            a = np.tanh(a * target_gain) / np.tanh(max(1.0, target_gain * peak)) * min(0.92, peak * target_gain)
            a = np.clip(a, -0.95, 0.95).astype(np.float32)
            diag.gain_applied_db = round(20.0 * math.log10(target_gain), 2)

    if pad_short_ms > 0 and 0 < len(a) < int(2.0 * dst_sr):
        pad = np.zeros(int(dst_sr * (pad_short_ms / 1000.0)), dtype=np.float32)
        a = np.concatenate([pad, a, pad]).astype(np.float32)

    return np.clip(a, -1.0, 1.0).astype(np.float32), diag

