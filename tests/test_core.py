"""Config, notes/graph, retrieval, history, persona: pure logic, no hardware."""
import json
import re

import pytest
from pydantic import ValidationError

from jarvis import persona
from jarvis.config import ROOT, Settings, load_settings, save_overrides
from jarvis.history import History
from jarvis.notes import NoteStore, slugify, title_from
from jarvis.retrieval import search, tokens


# ---- config ----
def test_default_config_valid():
    s = load_settings()
    assert s.host == "127.0.0.1" and s.brains["local"].model == "qwen3:4b-instruct" and s.brains["local"].num_ctx == 4096
    assert s.brains["local_thinking"].thinking is True


def test_config_rejects_bad_values():
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        Settings.model_validate({**raw, "boot_brain": "nope"})
    with pytest.raises(ValueError):
        Settings.model_validate({**raw, "host": "0.0.0.0"})  # never bind publicly
    with pytest.raises(ValidationError):
        Settings.model_validate({**raw, "brains": {"local": {"model": "x", "num_ctx": 10}}})


def test_overrides_merge_without_touching_config(tmp_path):
    ov = tmp_path / "settings.json"
    save_overrides({"tts": {"speed": 1.3}}, ov)
    save_overrides({"wake": {"threshold": 0.7}}, ov)
    s = load_settings(overrides_path=ov)
    assert s.tts.speed == 1.3 and s.wake.threshold == 0.7 and s.tts.voice == "en_GB-alan-medium"


# ---- notes / graph ----
def test_graph_ids_are_indexes_and_links_exist(notes_dir):
    g = NoteStore(notes_dir).graph()
    assert len(g["nodes"]) == 30 and [n["id"] for n in g["nodes"]] == list(range(30))
    assert {n["group"] for n in g["nodes"]} == {"programming", "technology", "personal", "learning"}
    assert len(g["links"]) > 30 and all(0 <= l["source"] < l["target"] < 30 for l in g["links"])


def test_memory_create_is_indexed_immediately_and_linked(notes_dir):
    st = NoteStore(notes_dir)
    n = st.add("the Pomodoro Technique helps me start", related=["Pomodoro Technique"])
    assert (notes_dir / n.path).exists() and "Captured" in (notes_dir / n.path).read_text(encoding="utf-8")
    assert n.category == "captures" and len(st.all()) == 31
    g = st.graph()
    pomo = next(x for x in g["nodes"] if x["label"] == "Pomodoro Technique")["id"]
    new = next(x for x in g["nodes"] if x["path"] == n.path)["id"]
    assert any({l["source"], l["target"]} == {pomo, new} for l in g["links"])
    assert search(st.all(), "pomodoro start")[0][0].title in {"Pomodoro Technique", n.title}


def test_memory_empty_and_duplicate_titles(notes_dir):
    st = NoteStore(notes_dir)
    with pytest.raises(ValueError):
        st.add("   ")
    a, b = st.add("same words here"), st.add("same words here")
    assert a.path != b.path


def test_update_and_safe_delete(notes_dir):
    st = NoteStore(notes_dir)
    n = st.add("temporary thought")
    st.update(n.id, "# Temporary thought\n\nchanged body")
    assert "changed body" in next(x for x in st.all() if x.path == n.path).body
    moved = st.delete(next(x for x in st.all() if x.path == n.path).id)
    assert not (notes_dir / n.path).exists() and (notes_dir / moved).exists()  # moved, never destroyed
    assert all(not x.path.startswith(".trash") for x in st.all())


def test_external_file_change_detected(notes_dir):
    st = NoteStore(notes_dir)
    v = st.version
    (notes_dir / "learning" / "Brand New.md").write_text("# Brand New\n\nlinks to [[Active Recall]]", encoding="utf-8")
    assert st.refresh(force=True) and st.version == v + 1 and len(st.all()) == 31


def test_helpers():
    assert slugify("Hello, World! ✓") == "hello-world" and title_from("one two three four five six seven eight") == "One two three four five six seven"


# ---- retrieval ----
def test_retrieval_ranks_relevant_first(notes_dir):
    st = NoteStore(notes_dir)
    assert search(st.all(), "how much VRAM does my GPU need", 3)[0][0].title == "GPU VRAM Basics"
    assert search(st.all(), "spaced repetition flashcards", 1)[0][0].title == "Spaced Repetition"
    assert search(st.all(), "zzzzqqq nonsense", 3) == [] and tokens("the and of") == []


# ---- history ----
def test_history_persists_windows_and_survives_torn_line(tmp_path):
    p = tmp_path / "h.jsonl"
    h = History(p, turns=2)
    for i in range(5):
        h.add("user", f"q{i}")
        h.add("assistant", f"a{i}")
    p.write_text(p.read_text(encoding="utf-8") + '{"role": "user", "con', encoding="utf-8")  # crash mid-write
    h2 = History(p, turns=2)
    w = h2.window()
    assert 2 <= len(w) <= 4 and w[0]["role"] == "user" and w[-1]["content"] == "a4"
    h2.add("user", "q5"), h2.add("assistant", "a5")
    assert h2.window()[:len(w)] == w  # window grows: the prompt prefix is unchanged, so the model cache is reused
    h2.clear()
    assert History(p).window() == []


def test_history_char_budget(tmp_path):
    h = History(tmp_path / "h.jsonl", turns=10)
    for i in range(10):
        h.add("user", "x" * 600), h.add("assistant", "y" * 600)
    assert sum(len(m["content"]) for m in h.window()) <= 2000


# ---- persona ----
def test_persona_budget_and_stable_prefix():
    assert len(persona.PERSONA.split()) < 300
    m1 = persona.build_messages([], "hi", [], "2026-01-01")
    m2 = persona.build_messages([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], "other", [("T", "x")], "2027-02-02")
    assert m1[0] == m2[0] and m1[0]["role"] == "system"  # identical, first: KV cache reuse
    assert "Notes:" in m2[-1]["content"] and "2027-02-02" in m2[-1]["content"] and "Notes:" not in m1[-1]["content"]
    assert re.search(r"never say you are Qwen", persona.PERSONA, re.I)
