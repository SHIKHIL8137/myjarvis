"""Push-to-talk dictation (Windows).

Hold the hotkey to record, release to transcribe, text is pasted into the focused window and the previous clipboard is restored.
Design constraints:
* reuses the Assistant's existing Whisper instance (never loads a second model) and the voice loop's mic is paused while recording
* the transcript goes ONLY to the paste target / local history - never to the wake word, the bus or the assistant conversation
* no third-party packages: hotkey state is polled with GetAsyncKeyState, paste uses the Win32 clipboard + SendInput
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("jarvis.ptt")

SAMPLE_RATE = 16_000
MAX_RECORD_S = 120
MIN_AUDIO_S = 0.25

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "space": 0x20, "enter": 0x0D, "tab": 0x09,
    "esc": 0x1B, "backspace": 0x08, **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): c for c in range(ord("a"), ord("z") + 1)}, **{str(d): 0x30 + d for d in range(10)},
}
_MODIFIERS = {"ctrl", "control", "alt", "shift", "win"}


def parse_hotkey(spec: str) -> list[int]:
    """'ctrl+alt+space' -> virtual-key codes. Requires at least one modifier so normal typing can never trigger dictation."""
    parts = [p for p in (spec or "").lower().replace(" ", "").split("+") if p]
    if len(parts) < 2 or not any(p in _MODIFIERS for p in parts):
        raise ValueError("hotkey needs at least one modifier (ctrl/alt/shift/win) plus a key")
    unknown = [p for p in parts if p not in VK]
    if unknown:
        raise ValueError(f"unknown key(s) in hotkey: {unknown}")
    return [VK[p] for p in parts]


# ---- hotkey state machine (pure, unit-testable) ----------------------------------------------------------
class HotkeyStateMachine:
    """Feed it the combo's down/up level; it emits 'start' / 'stop' / 'cancel' events.

    * hold: start on press, stop on release (a release sooner than `min_hold_s` is a tap -> 'cancel')
    * double-tap (optional): two taps within `double_tap_s` latch hands-free recording; the next press stops it
    """

    def __init__(self, hands_free: bool = False, min_hold_s: float = 0.30, double_tap_s: float = 0.50):
        self.hands_free, self.min_hold_s, self.double_tap_s = hands_free, min_hold_s, double_tap_s
        self.state = "idle"  # idle | hold | latched
        self._down = False
        self._down_at = 0.0
        self._last_tap_end = -1e9

    def update(self, down: bool, now: float) -> list[str]:
        ev: list[str] = []
        if down and not self._down:  # press edge
            self._down, self._down_at = True, now
            if self.state == "latched":
                self.state = "idle"
                ev.append("stop")
            elif self.hands_free and now - self._last_tap_end <= self.double_tap_s:
                self.state = "latched"
                self._last_tap_end = -1e9
                ev.append("start")
            else:
                self.state = "hold"
                ev.append("start")
        elif not down and self._down:  # release edge
            self._down = False
            if self.state == "hold":
                self.state = "idle"
                if now - self._down_at < self.min_hold_s:
                    self._last_tap_end = now
                    ev.append("cancel")
                else:
                    ev.append("stop")
            # latched: release after the second tap is ignored (recording continues hands-free)
        return ev


# ---- text cleanup -------------------------------------------------------------------------------------
_FILLER_RE = re.compile(r"(?<![\w'])(?:u+m+|u+h+|e+r+m*|a+h+m+|h+m+|m+h+m+)(?![\w'])[,.]?\s*", re.IGNORECASE)


def remove_fillers(text: str) -> str:
    out = _FILLER_RE.sub("", text or "")
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    out = re.sub(r"^[,.\s]+", "", out)
    if out and text and text[:1].isupper():
        out = out[0].upper() + out[1:]
    return out


def load_dictionary(path: Path) -> list[tuple[str, str]]:
    """config/dictionary.txt: `word` (fix casing) or `heard => written` per line; '#' comments."""
    pairs: list[tuple[str, str]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return pairs
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=>" in line:
            a, b = (x.strip() for x in line.split("=>", 1))
            if a and b:
                pairs.append((a, b))
        else:
            pairs.append((line, line))
    return pairs


def apply_dictionary(text: str, pairs: list[tuple[str, str]]) -> str:
    for heard, written in sorted(pairs, key=lambda p: -len(p[0])):
        text = re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", written.replace("\\", "\\\\"), text, flags=re.IGNORECASE)
    return text


def clean_dictation(text: str, *, filler: bool, dictionary: list[tuple[str, str]]) -> str:
    out = (text or "").strip()
    if filler:
        out = remove_fillers(out)
    return apply_dictionary(out, dictionary).strip()


# ---- history (text only) ------------------------------------------------------------------------------
class DictationHistory:
    def __init__(self, path: Path):
        self.path = Path(path)

    def add(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "text": text}, ensure_ascii=False) + "\n")

    def list(self, limit: int = 100) -> list[dict]:
        try:
            rows = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return []
        return rows[-limit:][::-1]

    def clear(self) -> int:
        n = len(self.list(10**9))
        self.path.unlink(missing_ok=True)
        return n


# ---- Win32 clipboard / paste ----------------------------------------------------------------------------
CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT, CF_LOCALE = 13, 1, 7, 16
_TEXT_FORMATS = {CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT, CF_LOCALE}
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x0002, 0x0004


class _KI(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MI(ctypes.Structure):  # pads the union to the real INPUT size (matters on 64-bit)
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _IU(ctypes.Union):
    _fields_ = [("ki", _KI), ("mi", _MI)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _IU)]


class Win32Clipboard:
    """Thin wrapper so the paste algorithm can be tested against a fake."""

    def __init__(self):
        self.u32 = ctypes.windll.user32
        self.k32 = ctypes.windll.kernel32
        self.u32.GetClipboardData.restype = ctypes.c_void_p
        self.u32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
        self.k32.GlobalAlloc.restype = ctypes.c_void_p
        self.k32.GlobalLock.restype = ctypes.c_void_p
        self.k32.GlobalLock.argtypes = [ctypes.c_void_p]
        self.k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self.k32.GlobalFree.argtypes = [ctypes.c_void_p]

    def _open(self) -> bool:
        for _ in range(10):
            if self.u32.OpenClipboard(None):
                return True
            time.sleep(0.02)
        return False

    def snapshot(self) -> dict:
        """{'empty': bool, 'text': str|None, 'foreign': bool} - foreign = holds non-text data we cannot faithfully restore."""
        if not self._open():
            return {"empty": False, "text": None, "foreign": True}
        try:
            fmts, f = [], 0
            while True:
                f = self.u32.EnumClipboardFormats(f)
                if not f:
                    break
                fmts.append(f)
            text = None
            if CF_UNICODETEXT in fmts:
                h = self.u32.GetClipboardData(CF_UNICODETEXT)
                p = self.k32.GlobalLock(h) if h else None
                if p:
                    text = ctypes.wstring_at(p)
                    self.k32.GlobalUnlock(h)
            return {"empty": not fmts, "text": text, "foreign": any(x not in _TEXT_FORMATS for x in fmts)}
        finally:
            self.u32.CloseClipboard()

    def set_text(self, text: str | None) -> bool:
        if not self._open():
            return False
        try:
            self.u32.EmptyClipboard()
            if text is None:
                return True
            data = (text + "\x00").encode("utf-16-le")
            h = self.k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            p = self.k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            self.k32.GlobalUnlock(h)
            if not self.u32.SetClipboardData(CF_UNICODETEXT, h):
                self.k32.GlobalFree(h)
                return False
            return True
        finally:
            self.u32.CloseClipboard()

    def send_ctrl_v(self) -> None:
        seq = [(VK["ctrl"], 0), (ord("V"), 0), (ord("V"), KEYEVENTF_KEYUP), (VK["ctrl"], KEYEVENTF_KEYUP)]
        arr = (_INPUT * len(seq))()
        for i, (vk, fl) in enumerate(seq):
            arr[i].type, arr[i].u.ki.wVk, arr[i].u.ki.dwFlags = INPUT_KEYBOARD, vk, fl
        self.u32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))

    def type_text(self, text: str) -> None:
        """Clipboard-free fallback: inject UTF-16 units as Unicode key events."""
        units = text.encode("utf-16-le")
        codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
        arr = (_INPUT * (len(codes) * 2))()
        for i, c in enumerate(codes):
            for j, fl in enumerate((KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)):
                e = arr[i * 2 + j]
                e.type, e.u.ki.wVk, e.u.ki.wScan, e.u.ki.dwFlags = INPUT_KEYBOARD, 0, c, fl
        self.u32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))


def paste_text(text: str, clip, *, settle_s: float = 0.15, sleep=time.sleep) -> str:
    """Paste via clipboard and restore the previous text clipboard. Returns 'clipboard' or 'typed'.

    If the clipboard holds non-text data (image, files...) it cannot be restored faithfully, so the text is typed instead.
    """
    snap = clip.snapshot()
    if snap.get("foreign"):
        clip.type_text(text)
        return "typed"
    if not clip.set_text(text):
        clip.type_text(text)
        return "typed"
    try:
        clip.send_ctrl_v()
        sleep(settle_s)
    finally:
        clip.set_text(snap["text"] if not snap["empty"] else None)  # restore previous text, or leave the clipboard empty
    return "clipboard"


# ---- service -------------------------------------------------------------------------------------------
@dataclass
class DictationStatus:
    enabled: bool
    running: bool
    recording: bool
    hotkey: str
    last_error: str


class DictationService:
    """Owns the hotkey poller + recorder. `stt` is the Assistant's existing Whisper object (shared, never copied)."""

    def __init__(
        self,
        stt,
        spec,
        *,
        root: Path,
        data_dir: Path,
        on_recording: Callable[[bool], None] | None = None,
        clipboard=None,
        key_down: Callable[[list[int]], bool] | None = None,
        recorder: Callable[[threading.Event], np.ndarray] | None = None,
        input_device: Callable[[], int | str | None] | None = None,
    ):
        self.stt, self.spec, self.root = stt, spec, Path(root)
        self.history = DictationHistory(Path(data_dir) / "dictation_history.jsonl")
        self.on_recording = on_recording or (lambda active: None)
        self._clip = clipboard
        self._key_down = key_down or self._win_key_down
        self._recorder = recorder or self._record
        self._input_device = input_device or (lambda: None)  # follows Settings.audio.input_device, like the voice loop
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rec_stop = threading.Event()
        self._rec_thread: threading.Thread | None = None
        self.recording = False
        self.last_error = ""
        self.last_text_len = 0

    # -- lifecycle
    def start(self) -> None:
        if not self.spec.enabled or (self._thread and self._thread.is_alive()):
            return
        keys = parse_hotkey(self.spec.hotkey)
        self._stop.clear()
        sm = HotkeyStateMachine(hands_free=self.spec.double_tap_hands_free)
        self._thread = threading.Thread(target=self._poll, args=(keys, sm), daemon=True, name="dictation-hotkey")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._rec_stop.set()
        for t in (self._thread, self._rec_thread):
            if t and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3)
        self._thread = self._rec_thread = None

    def reconfigure(self, spec) -> None:
        self.stop()
        self.spec = spec
        self.start()

    def status(self) -> DictationStatus:
        return DictationStatus(bool(self.spec.enabled), bool(self._thread and self._thread.is_alive()), self.recording, self.spec.hotkey, self.last_error)

    # -- hotkey loop
    def _poll(self, keys: list[int], sm: HotkeyStateMachine) -> None:
        while not self._stop.is_set():
            try:
                for ev in sm.update(self._key_down(keys), time.monotonic()):
                    self.handle_event(ev)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                log.exception("dictation poll error")
            time.sleep(0.015)

    def handle_event(self, ev: str) -> None:
        if ev == "start" and not self.recording:
            self._begin()
        elif ev in ("stop", "cancel") and self.recording:
            self._finish(discard=(ev == "cancel"))

    def _begin(self) -> None:
        self.recording = True
        self._rec_stop.clear()
        try:
            self.on_recording(True)  # pauses TTS and the voice loop's mic
        except Exception:  # noqa: BLE001
            log.exception("on_recording(True) failed")
        self._rec_result: list[np.ndarray] = []
        self._rec_thread = threading.Thread(target=lambda: self._rec_result.append(self._recorder(self._rec_stop)), daemon=True, name="dictation-rec")
        self._rec_thread.start()

    def _finish(self, *, discard: bool) -> None:
        self._rec_stop.set()
        t = self._rec_thread
        if t:
            t.join(timeout=5)
        self.recording = False
        try:
            self.on_recording(False)
        except Exception:  # noqa: BLE001
            log.exception("on_recording(False) failed")
        audio = self._rec_result[0] if self._rec_result else np.empty(0, dtype=np.float32)
        if discard or audio.size < MIN_AUDIO_S * SAMPLE_RATE:
            return
        threading.Thread(target=self._transcribe_and_paste, args=(audio,), daemon=True, name="dictation-stt").start()

    # -- pipeline (synchronous; unit-tested directly)
    def process_audio(self, audio: np.ndarray) -> str:
        res = self.stt.transcribe(audio, language="en", save_audio=False, allow_retry=False)  # shared Whisper, English preset
        raw = (res.get("text", "") if isinstance(res, dict) else str(res or "")).strip()
        return clean_dictation(raw, filler=self.spec.remove_fillers, dictionary=load_dictionary(self._dict_path()))

    def _dict_path(self) -> Path:
        p = Path(self.spec.dictionary_file)
        return p if p.is_absolute() else self.root / p

    def _transcribe_and_paste(self, audio: np.ndarray) -> None:
        try:
            text = self.process_audio(audio)
            if not text:
                return
            self.last_text_len = len(text)
            paste_text(text, self._clip or Win32Clipboard())
            if self.spec.history_enabled:
                self.history.add(text)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:200]
            log.exception("dictation failed")

    # -- Win32 pieces
    @staticmethod
    def _win_key_down(keys: list[int]) -> bool:
        if sys.platform != "win32":
            return False
        u32 = ctypes.windll.user32
        return all(u32.GetAsyncKeyState(k) & 0x8000 for k in keys)

    def _record(self, stop: threading.Event) -> np.ndarray:
        import sounddevice as sd

        q: queue.Queue[np.ndarray] = queue.Queue()
        frames: list[np.ndarray] = []
        deadline = time.monotonic() + MAX_RECORD_S
        try:
            from .voice import _resolve_input_device

            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=_resolve_input_device(sd, self._input_device()), callback=lambda d, *_: q.put(d.copy())):
                while not stop.is_set() and time.monotonic() < deadline:
                    time.sleep(0.02)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"microphone: {e}"[:200]
            log.exception("dictation recording failed")
        while not q.empty():
            frames.append(q.get_nowait())
        return np.concatenate(frames).reshape(-1) if frames else np.empty(0, dtype=np.float32)
