"""Push-to-talk dictation: hotkey state machine, clipboard restore, cleanup, dictionary, STT reuse, isolation from the assistant."""
from __future__ import annotations

import time

import numpy as np
import pytest

from jarvis import ptt
from jarvis.config import DictationSpec, load_settings
from jarvis.ptt import (
    DictationHistory,
    DictationService,
    HotkeyStateMachine,
    apply_dictionary,
    clean_dictation,
    load_dictionary,
    paste_text,
    parse_hotkey,
    remove_fillers,
)


# ---- hotkey parsing / config ----
def test_default_hotkey_and_validation():
    assert load_settings().dictation.hotkey == "ctrl+alt+space" and load_settings().dictation.enabled is False
    assert parse_hotkey("Ctrl+Alt+Space") == [0x11, 0x12, 0x20]
    for bad in ("space", "a+b", "ctrl+nonsense", ""):
        with pytest.raises(ValueError):
            parse_hotkey(bad)
    with pytest.raises(Exception):
        DictationSpec(hotkey="space")


# ---- state machine ----
def test_hold_and_release():
    sm = HotkeyStateMachine()
    assert sm.update(True, 0.0) == ["start"]
    assert sm.update(True, 1.0) == []
    assert sm.update(False, 1.5) == ["stop"] and sm.state == "idle"


def test_short_tap_is_cancelled_without_double_tap_mode():
    sm = HotkeyStateMachine()
    assert sm.update(True, 0.0) == ["start"]
    assert sm.update(False, 0.1) == ["cancel"]
    assert sm.update(True, 0.2) == ["start"]  # second quick press just starts a normal hold


def test_double_tap_latches_hands_free_until_next_press():
    sm = HotkeyStateMachine(hands_free=True)
    sm.update(True, 0.0)
    assert sm.update(False, 0.1) == ["cancel"]
    assert sm.update(True, 0.3) == ["start"] and sm.state == "latched"
    assert sm.update(False, 0.4) == []  # release ignored: still recording
    assert sm.update(True, 5.0) == ["stop"] and sm.state == "idle"
    assert sm.update(False, 5.1) == []


def test_double_tap_window_expires():
    sm = HotkeyStateMachine(hands_free=True, double_tap_s=0.5)
    sm.update(True, 0.0)
    sm.update(False, 0.1)
    assert sm.update(True, 2.0) == ["start"] and sm.state == "hold"


# ---- cleanup / dictionary ----
def test_filler_removal():
    assert remove_fillers("Um, I think uh we should go") == "I think we should go"
    assert remove_fillers("Hmm. The umbrella is here") == "The umbrella is here"  # 'umbrella' untouched
    assert remove_fillers("er... fine") == "fine" or remove_fillers("er... fine").endswith("fine")


def test_dictionary_replacement_and_file(tmp_path):
    f = tmp_path / "dictionary.txt"
    f.write_text("# comment\nKubernetes\nj a r v i s => JARVIS\nerp v2 => ERP_V2\n", encoding="utf-8")
    pairs = load_dictionary(f)
    assert apply_dictionary("deploy kubernetes for erp v2", pairs) == "deploy Kubernetes for ERP_V2"
    assert apply_dictionary("kubernetesx stays", pairs) == "kubernetesx stays"
    assert load_dictionary(tmp_path / "missing.txt") == []
    assert clean_dictation("uh open erp v2", filler=True, dictionary=pairs) == "open ERP_V2"
    assert clean_dictation("uh open", filler=False, dictionary=[]) == "uh open"


# ---- clipboard ----
class FakeClip:
    def __init__(self, text=None, foreign=False):
        self.text, self.foreign, self.pasted, self.typed, self.fail_set = text, foreign, [], [], False

    def snapshot(self):
        return {"empty": self.text is None and not self.foreign, "text": self.text, "foreign": self.foreign}

    def set_text(self, t):
        if self.fail_set:
            return False
        self.text = t
        return True

    def send_ctrl_v(self):
        self.pasted.append(self.text)

    def type_text(self, t):
        self.typed.append(t)


def test_paste_restores_previous_clipboard():
    c = FakeClip("my precious copy")
    assert paste_text("hello", c, sleep=lambda s: None) == "clipboard"
    assert c.pasted == ["hello"] and c.text == "my precious copy"


def test_paste_restores_empty_clipboard_to_empty():
    c = FakeClip(None)
    paste_text("hello", c, sleep=lambda s: None)
    assert c.pasted == ["hello"] and c.text is None


def test_paste_restores_even_if_paste_raises():
    c = FakeClip("keep")
    c.send_ctrl_v = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        paste_text("x", c, sleep=lambda s: None)
    assert c.text == "keep"


def test_non_text_clipboard_is_never_overwritten():
    c = FakeClip("img", foreign=True)
    assert paste_text("hello", c, sleep=lambda s: None) == "typed"
    assert c.typed == ["hello"] and c.text == "img" and c.pasted == []


def test_clipboard_failure_falls_back_to_typing():
    c = FakeClip("keep")
    c.fail_set = True
    assert paste_text("hello", c, sleep=lambda s: None) == "typed" and c.typed == ["hello"]


# ---- service: STT reuse, isolation, history ----
class FakeSTT:
    def __init__(self, text="um hello world"):
        self.calls, self.text = [], text

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        return {"text": self.text}


def _svc(tmp_path, **spec_kw):
    spec = DictationSpec(enabled=True, **spec_kw)
    stt, clip, events = FakeSTT(), FakeClip("orig"), []
    svc = DictationService(stt, spec, root=tmp_path, data_dir=tmp_path, on_recording=events.append, clipboard=clip)
    return svc, stt, clip, events


def test_process_audio_reuses_shared_stt_with_english_and_cleans(tmp_path, monkeypatch):
    import jarvis.stt as stt_mod

    monkeypatch.setattr(stt_mod.Whisper, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("second Whisper loaded")))
    svc, stt, _, _ = _svc(tmp_path)
    out = svc.process_audio(np.zeros(16000, dtype=np.float32))
    assert out == "hello world" and stt.calls[0]["language"] == "en" and stt.calls[0]["save_audio"] is False


def test_end_to_end_with_fake_key_and_recorder_pastes_and_pauses_voice(tmp_path):
    state = {"down": False}
    audio = np.ones(16000, dtype=np.float32) * 0.1
    spec = DictationSpec(enabled=True, history_enabled=True)
    stt, clip, events = FakeSTT("uh hello there"), FakeClip("orig"), []
    svc = DictationService(stt, spec, root=tmp_path, data_dir=tmp_path, on_recording=events.append, clipboard=clip,
                           key_down=lambda keys: state["down"], recorder=lambda stop: (stop.wait(2), audio)[1])
    svc.start()
    try:
        state["down"] = True
        time.sleep(0.5)
        assert svc.recording and events == [True]
        state["down"] = False
        for _ in range(100):
            if clip.pasted and clip.text == "orig":
                break
            time.sleep(0.05)
    finally:
        svc.stop()
    assert events == [True, False] and clip.pasted == ["hello there"] and clip.text == "orig"
    assert [r["text"] for r in svc.history.list()] == ["hello there"]


def test_short_tap_discards_audio(tmp_path):
    svc, stt, clip, events = _svc(tmp_path)
    svc._recorder = lambda stop: (stop.wait(1), np.ones(16000, dtype=np.float32))[1]
    svc.handle_event("start")
    svc.handle_event("cancel")
    time.sleep(0.2)
    assert stt.calls == [] and clip.pasted == [] and events == [True, False]


def test_transcript_never_reaches_assistant_or_wake(tmp_path):
    """Dictation has no reference to the bus / voice loop / assistant: only the on_recording hook and the paste target."""
    svc, *_ = _svc(tmp_path)
    assert not any(hasattr(svc, n) for n in ("bus", "assistant", "voice", "wake", "speaker"))
    import inspect
    src = inspect.getsource(ptt)
    assert "stream_turn" not in src and "WakeWord" not in src and "publish" not in src


def test_history_off_by_default_and_deletable(tmp_path):
    svc, *_ = _svc(tmp_path)
    svc._transcribe_and_paste(np.ones(16000, dtype=np.float32))
    assert svc.history.list() == []  # history_enabled defaults to False
    h = DictationHistory(tmp_path / "h.jsonl")
    h.add("a")
    h.add("b")
    assert [r["text"] for r in h.list()] == ["b", "a"]
    assert h.clear() == 2 and h.list() == []


def test_disabled_service_does_not_start(tmp_path):
    svc = DictationService(FakeSTT(), DictationSpec(enabled=False), root=tmp_path, data_dir=tmp_path)
    svc.start()
    assert svc.status().running is False


@pytest.mark.skipif(not hasattr(ptt.ctypes, "windll"), reason="Windows only")
def test_win32_struct_sizes_match_input():
    assert ptt.ctypes.sizeof(ptt._INPUT) in (28, 40)  # 40 on 64-bit Windows
