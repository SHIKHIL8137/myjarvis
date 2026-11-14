"""Regression tests for the "unrelated ERP / ELCD context in answers" defect and the honesty of local-file answers.

Root causes (reproduced on a copy of the real database): memory injection was gated on a composite score that contains query-independent
terms (recency, confidence), every spoken utterance - including Whisper hallucinations - was stored as a 'conversation memory', any short
message borrowed the previous question for retrieval, and a single generic word ("API") pulled an unrelated note into the prompt."""
from __future__ import annotations

import asyncio

from jarvis.brain import Brain
from jarvis.retrieval import (
    claims_unperformed_action,
    grounded,
    is_followup,
    no_context_requested,
    refers_to_local_artifact,
    worth_remembering,
)


def test_grounded_requires_query_dependent_overlap():
    assert not grounded("What is the capital of France", "first u can understand the erp project")
    assert not grounded("Open my OpenAI API project folder", "first u can understand the erp project")  # only the generic word "project"
    assert grounded("what did I say about the erp project", "first u can understand the erp project")
    assert grounded("Which database stack do I prefer", "I prefer PostgreSQL", min_shared=1, frac=0.0)
    assert not grounded("", "anything")


def test_followup_detection_does_not_borrow_for_standalone_short_requests():
    for standalone in ("stop", "open my folder", "what is 2 plus 2", "Find the OpenAI API config", "turn off the lights"):
        assert not is_followup(standalone), standalone
    for follow in ("and the database?", "what about it", "why", "tell me more about that", "again"):
        assert is_followup(follow), follow


def test_explicit_no_context_requests():
    for t in ("Ignore previous context. What is 2+2?", "new topic: cooking", "don't use my memory for this", "forget the earlier conversation", "start over"):
        assert no_context_requested(t), t
    for t in ("Explain this error", "what did we discuss", "remember the previous answer"):
        assert not no_context_requested(t), t


def test_noise_and_questions_are_not_remembered():
    for junk in ("Terima kasih kerana menonton, terima kasih kerana menonton.", "Ollama, Qwen, Qwen, Qwen, Qwen, Qwen, Qwen, Qwen",
                 "Turning off the message from the ELCD", "how many model we have currently now", "I can't, I can't, I can't, I can't.",
                 "Hello Jarvis, can you confirm you are connected to OpenRouter?", "first u can understand the erp project"):
        assert not worth_remembering(junk), junk
    for fact in ("I prefer dark mode in all my editors", "My project uses PostgreSQL for the ERP backend", "We deploy the website every Friday evening"):
        assert worth_remembering(fact), fact


def test_local_artifact_detection_and_fabricated_action_claims():
    for t in ("Find the OpenAI API configuration file.", "Explain how my OpenAI API integration works.", "Search my project for OpenAI API references."):
        assert refers_to_local_artifact(t), t
    for t in ("What is the capital of France", "Explain REST versus GraphQL", "hello"):
        assert not refers_to_local_artifact(t), t
    assert claims_unperformed_action("I attempted to search for relevant files, but no matches were found.", [])
    assert not claims_unperformed_action("I attempted to search for relevant files.", ["fs_search_files"])
    assert not claims_unperformed_action("Sir, I do not have access to that folder.", [])


# ---------------- through the real Assistant ----------------
class _Capture(Brain):
    def __init__(self, settings, reply="Certainly."):
        super().__init__(settings)
        self.reply, self.seen = reply, []

    async def installed(self):
        return ["qwen3:4b-instruct"]

    async def health(self):
        return {"up": True, "version": "fake", "models": await self.installed(), "loaded": []}

    async def stream_events(self, messages, tools=None, **kw):
        self.seen.append({"messages": messages, "tools": [t["function"]["name"] for t in (tools or [])]})
        yield {"type": "token", "text": self.reply}
        self.last_stats = {"ttft": 0.1, "tokens": 3, "tps": 30.0, "total": 0.2, "model": "m", "truncated": False}

    async def stream(self, messages):
        self.seen.append({"messages": messages, "tools": []})
        yield self.reply


def _mem(a, text, category="conversation_memory", source="conversation_derived"):
    return a.orchestrator.memory.record_adaptive_memory(content=text, category=category, topic="t", title=text[:40], source=source)


def test_unrelated_question_gets_no_memories_but_related_one_does(assistant):
    a = assistant
    _mem(a, "first u can understand the erp project")
    _mem(a, "Executed workflow 'general_chat': ERP_V2", category="command_memory", source="command_derived")
    _mem(a, "Terima kasih kerana menonton, terima kasih kerana menonton.")
    for q in ("What is the capital of France", "Find the OpenAI API configuration file", "turn off the lights"):
        assert a._gather_context(q)["memories_ctx"] == [], q
    related = a._gather_context("what did I say about the erp project")["memories_ctx"]
    assert [t for _, t in related] == ["first u can understand the erp project"]  # relevant: kept; the command log is never injected


def test_short_message_does_not_borrow_previous_question(assistant):
    a = assistant
    _mem(a, "My ERP backend uses PostgreSQL and FastAPI")
    a.history.add("user", "tell me about the erp backend postgresql fastapi")
    a.history.add("assistant", "It uses PostgreSQL.")
    assert a._gather_context("stop")["memories_ctx"] == []
    assert a._gather_context("open my folder")["memories_ctx"] == []
    assert a._gather_context("and the database?")["memories_ctx"]  # a genuine follow-up still gets the previous topic


def test_ignore_previous_context_drops_history_memory_and_notes(assistant, settings):
    a = assistant
    cap = _Capture(settings)
    a.brain = cap
    a.history.add("user", "tell me about the erp project")
    a.history.add("assistant", "ERP_SECRET_CONTEXT answer")
    ctx = a._gather_context("anything about the erp project", use_context=False)
    assert ctx["memories_ctx"] == [] and ctx["notes_ctx"] == [] and ctx["history_ctx"] == []

    async def run():
        return [e async for e in a.stream_turn("Ignore previous context. Explain what a mutex is.", speak=False)]

    asyncio.run(run())
    sent = "\n".join(str(m.get("content")) for call in cap.seen for m in call["messages"])
    assert "ERP_SECRET_CONTEXT" not in sent


def test_follow_up_still_receives_conversation_history(assistant, settings):
    a = assistant
    cap = _Capture(settings)
    a.brain = cap
    a.history.add("user", "my favourite colour is teal")
    a.history.add("assistant", "Noted: teal.")

    async def run():
        return [e async for e in a.stream_turn("what did I just say my favourite colour was", speak=False)]

    asyncio.run(run())
    sent = "\n".join(str(m.get("content")) for call in cap.seen for m in call["messages"])
    assert "favourite colour is teal" in sent


def test_conversation_memory_is_only_written_for_real_statements(assistant):
    a = assistant
    mem = a.orchestrator.memory
    before = len(mem.list_personal_memories())
    for junk in ("Terima kasih kerana menonton, terima kasih kerana menonton.", "Turning off the message from the ELCD", "can you confirm you are connected to OpenRouter please?"):
        a._record_adaptive_turn_memory(junk, intent="general_chat")
    assert len(mem.list_personal_memories()) == before
    a._record_adaptive_turn_memory("I prefer dark mode in all my editors", intent="general_chat")
    assert len(mem.list_personal_memories()) == before + 1


def test_fabricated_file_search_claim_is_flagged_and_tools_are_offered(assistant, settings):
    a = assistant
    cap = _Capture(settings, reply="I attempted to search for the file, but no matches were found.")
    a.brain = cap

    async def run():
        return [e async for e in a.stream_turn("Find the OpenAI API configuration file.", speak=False)]

    evs = asyncio.run(run())
    done = next(e for e in evs if e.get("type") == "done")
    assert "Honesty notice" in done["answer"]  # no tool ran, so the claim must not stand unqualified
    assert any(n.startswith("fs_") for n in cap.seen[0]["tools"])  # the filesystem tools were offered for a local-file request
    assert "OWN local files" in "\n".join(str(m.get("content")) for m in cap.seen[0]["messages"])


def test_search_tool_defaults_to_the_workspace_root_and_retries_with_words(assistant):
    """fs_search_files with no path used to fall through to an unrelated folder; it now uses the configured workspace root, and a guessed
    file name is retried word by word."""
    import json
    from types import SimpleNamespace

    from jarvis.config import ROOT

    a = assistant
    a.settings.filesystem.workspace_roots = [str(ROOT / "docs")]  # not a temp dir, so it is accepted as a trusted root
    res = asyncio.run(a._resolve_fs_tool_target(""))
    assert res["status"] == "resolved" and res["path"] == str(ROOT / "docs")

    calls: list[str] = []

    async def fake_search(target, query="", task_id=None):
        calls.append(query)
        return [{"path": f"{target}/openai_client.py", "name": "openai_client.py"}] if query == "openai" else []

    async def allow(target, op, task_id=None):
        return SimpleNamespace(blocked=False, allowed=True, canonical_path=target, code="", reason="")

    a.orchestrator.fs_access.execute_search_files = fake_search
    a.orchestrator.fs_access.authorize = allow
    out = json.loads(asyncio.run(a._execute_fs_tool("fs_search_files", {"query": "openai_api_config.json"})))
    assert out["ok"] and out["count"] == 1 and out["query_used"] == "openai"
    assert calls[0] == "openai_api_config.json" and "openai" in calls


def test_stale_folder_clarification_does_not_hijack_unrelated_messages(assistant):
    """Regression: after 'I couldn't locate `documants`...' every following message (even 'wat is ollama') got the same stale question."""
    a = assistant

    def pending(cands=()):
        return a._build_pending_clarification(
            mode="select_candidate" if cands else "supply_parent_dir", original_request="show me files in my documants folder",
            original_intent="filesystem_ops", fs_info={}, project_name="documants", candidates=list(cands), task_id=None,
        )

    async def reply(text, p):
        a.pending_clarification = p
        res = await a._handle_pending_clarification_reply(text, p, source="text")
        return res, a.pending_clarification

    for unrelated in ("how much ram am i using", "wat is ollama?", "explain how fastapi works", "remember that my favourite editor is neovim", "what is 2 plus 2"):
        res, left = asyncio.run(reply(unrelated, pending()))
        assert res is None and left is None, unrelated  # routed normally, old question dropped
    cands = [{"index": 1, "id": "c1", "name": "docs_a", "path": "D:/projects/docs_a", "source": "workspace", "score": 1},
             {"index": 2, "id": "c2", "name": "docs_b", "path": "D:/projects/docs_b", "source": "workspace", "score": 1}]
    for answer in ("2", "the second one", "docs_a", "D:/projects"):
        p = pending(cands)
        a.pending_clarification = p
        assert a._looks_like_clarification_answer(answer, cands), answer  # genuine answers are still consumed


def test_history_reset_endpoint_drops_a_pending_clarification(settings, assistant, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis.api import create_app

    monkeypatch.setattr("jarvis.api.save_overrides", lambda p, *a: None)
    assistant.pending_clarification = {"task_id": None, "candidates": []}
    with TestClient(create_app(settings, assistant)) as c:
        assert c.delete("/api/history", headers={"X-Jarvis": "1"}).status_code == 200
    assert assistant.pending_clarification is None


def test_natural_phrasings_reach_the_right_handlers():
    """Rule families (not phrase lists) for: remembering facts, resource questions about THIS machine, online look-ups, finding a document by type."""
    from jarvis.decision.rules import FILESYSTEM_OPS_RE, SYSTEM_STATUS_RE, WEB_RESEARCH_RE
    from jarvis.intent import REMEMBER, is_stop_typo

    for t in ("Save a note that the meeting moved to Friday", "Make a note: I prefer concise replies", "Please keep in mind that I like short answers",
              "pls dont forget i hate mondays", "remeber that my editor is neovim", "Remember that my dog is called Biscuit"):
        assert REMEMBER.match(t), t
    assert REMEMBER.match("Keep in mind that I like tea").group(1) == "I like tea"
    assert not REMEMBER.match("I remember when we met") and not REMEMBER.match("Can you remember what a mutex is?")
    for t in ("how much ram am i using", "How busy is my processor?", "Is my CPU under heavy load?", "how much vram do I have left", "my disk space"):
        assert SYSTEM_STATUS_RE.search(t), t
    for t in ("How much VRAM do I need for a local model?", "What is a CPU?", "Explain RAM", "Tell me about memory management in Python"):
        assert not SYSTEM_STATUS_RE.search(t), t  # general knowledge questions must stay conversation
    assert WEB_RESEARCH_RE.search("Look up recent news about local LLMs online") and WEB_RESEARCH_RE.search("serch for python 3.14 notes on the web")
    assert FILESYSTEM_OPS_RE.search("wanna find that pdf i got last week") and not FILESYSTEM_OPS_RE.search("what is a pdf")
    assert is_stop_typo("stoop") and is_stop_typo("cancle") and not is_stop_typo("status") and not is_stop_typo("stop the music now please")


def test_routing_typo_repair_is_limited_to_command_words():
    from jarvis.typo import normalize_for_routing as n

    assert n("waht is the cpu temprature") == "what is the cpu temperature"
    assert n("show me the fiels in my downlods") == "show me the files in my downloads"
    for ordinary in ("show me older emails", "how do flies fly", "the form from there"):
        assert n(ordinary) == ordinary


def test_context_forget_and_polite_stop_and_recall_paraphrases():
    from jarvis.intent import FORGET_MEMORY, RECALL_MEMORY, RECALL_PREFERENCE, STOP, _CONTEXT_FORGET, is_stop_typo  # noqa: F401

    assert _CONTEXT_FORGET.search("Forget everything we said before and tell me what a deadlock is")
    assert all(STOP.match(t) for t in ("quiet please", "okay stop", "enough"))
    assert not STOP.match("stop the music")
    assert RECALL_MEMORY.match("Do you remember what I said about my ERP backend?")
    assert RECALL_PREFERENCE.match("What editor do I like?")


def test_short_query_with_one_distinctive_shared_word_is_grounded():
    from jarvis.retrieval import grounded

    assert grounded("versus quantization", "Quantization reduces model precision")
    assert not grounded("good morning", "Quantization reduces model precision")
