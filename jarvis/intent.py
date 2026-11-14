"""Centralized intent routing & deterministic command handling (Phases 1, 10, 11, 14):
routes explicit voice/text commands, permission-checked actions, memory/history operations,
voice/model switching, filesystem search/inspection, and learning/training lifecycle controls."""
import asyncio
import re

from . import audit
from .brain import BrainError
from .filesystem import FilesystemPermissionError
from .learning.feedback import is_correction_statement
from .memory_store import infer_memory_category
from .retrieval import search
from .search import SearchOfflineError

# Re-exported so existing callers referencing commands.WEB_EXPLICIT work seamlessly
WEB_EXPLICIT = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:search\s+(?:the\s+)?(?:web|internet|online)|look\s+up\s+online|google)\b",
    re.I,
)
REMEMBER = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+|pls\s+|kindly\s+)?(?:(?:remember|rem(?:em|e)b[ae]r)(?:\s+that|\s+this)?|"
    r"(?:do\s*n[o']?t|don'?t|do\s+not)\s+forget(?:\s+that)?|keep\s+in\s+mind(?:\s+that)?|"
    r"(?:make|take|save|add)\s+(?:a\s+)?(?:quick\s+)?note(?:\s+that)?|(?:save|add)\s+(?:this\s+)?to\s+(?:my\s+)?memory|"
    r"note\s+(?:down\s+)?that|write\s+down\s+that|take\s+note\s+that|"
    r"(?:i\s+(?:want|need|would\s+like)\s+you\s+to|i'?d\s+like\s+you\s+to)\s+(?:please\s+)?remember(?:\s+that)?|(?:can|could|would)\s+you\s+(?:please\s+)?remember\s+that|"
    r"just\s+so\s+(?:you|u)\s+know(?:\s+that)?|for\s+your\s+information)\b[:\s,-]*(.*)$",
    re.I | re.S,
)
STOP = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|alright|now|just|please|hey)\W+)*(?:jarvis\W+)?(?:please\W+)?"
    r"(?:stop(?:\s+talking|\s+speaking)?|quiet|silence|shut\s+up|cancel|enough|be\s+quiet|that'?s\s+enough)(?:\W+(?:please|now|already|it|that|this|jarvis))*\W*$",
    re.I,
)


def is_stop_typo(text: str) -> bool:
    """A single mistyped stop word ('stoop', 'cancle', 'quite'): close to a stop command, and nothing else was said."""
    import difflib

    words = re.findall(r"[a-z]+", (text or "").lower())
    return len(words) == 1 and len(words[0]) >= 4 and bool(difflib.get_close_matches(words[0], ["stop", "cancel", "quiet", "silence"], n=1, cutoff=0.8))

PAUSE_VOICE = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:pause|mute)\s+(?:listening|voice|microphone|mic)\W*$", re.I)
RESUME_VOICE = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:resume|unmute|start)\s+(?:listening|voice|microphone|mic)\W*$", re.I)

VOICE_LIST = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:list|show|what)\s+(?:all\s+)?(?:available\s+)?voices\W*$", re.I)
VOICE_SWITCH = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:switch|change|use|set)\s+(?:the\s+)?(?:tts\s+)?voice(?:\s+to)?\s+(.+)$",
    re.I,
)
VOICE_RATE = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:speak\s+(faster|slower)|(?:set\s+)?(?:voice|speech)\s+(?:rate|speed)\s+(?:to\s+)?([0-9]+(?:\.[0-9]+)?))\W*$",
    re.I,
)
SWITCH_MODEL = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:switch|change|use)\s+(?:to\s+)?(?:the\s+)?(?!voice\b|personality\b|tone\b|style\b)(.+?)(?:\s+model)?\W*$",
    re.I,
)
WHICH_MODEL = re.compile(r"^\W*(?:what|which)\s+model(?:\s+(?:is\s+(?:this|active)|are\s+you\s+using))?\W*$", re.I)
COUNT_NOTES = re.compile(r"\bhow\s+many\s+(?:notes|memories)\b", re.I)
SHOW_NOTES = re.compile(r"^\W*(?:show|open|list)\s+(?:all\s+|my\s+)?notes\W*$", re.I)
FIND_NOTES = re.compile(
    r"^\W*(?:find|search)\s+(?:my\s+)?(?:(?:notes|knowledge)(?:\s+(?:about|for|on))?\s*(.*)|information\s+(?:about|on|for)\s+(.+))$",
    re.I,
)

# Phase 2 & 10 memory / history / correction commands
UPDATE_MEMORY = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?update\s+memory\s+([a-z0-9_-]+)\s+(?:to|=|:)\s+(.+)$",
    re.I | re.S,
)
FORGET_MEMORY = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:forget(?:\s+about)?|delete\s+memory)\s+(.+)$",
    re.I,
)
RECALL_MEMORY = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:what\s+do\s+you\s+remember\s+about|tell\s+me\s+what\s+you\s+remember\s+about|"
    r"do\s+you\s+remember(?:\s+(?:what|anything|that))?(?:\s+i\s+(?:said|told\s+you))?\s+about|what\s+did\s+i\s+(?:tell\s+you|say)\s+about|"
    r"search\s+(?:my\s+)?memory\s+(?:for|about)|recall)\s+(.+)$",
    re.I,
)
RECALL_PREFERENCE = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?what\s+(\w+)\s+do\s+i\s+(?:like|prefer|use|love|hate)\W*$", re.I)
# "forget everything we said before ..." asks to drop CONTEXT, it must never delete a stored memory
_CONTEXT_FORGET = re.compile(r"\b(?:we\s+(?:said|discussed|talked)|earlier|previous(?:ly)?|before|everything|conversation|context|chat)\b", re.I)
SEARCH_HISTORY = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:search\s+(?:conversation\s+)?history\s+(?:for|about)|what\s+did\s+we\s+discuss(?:\s+about)?)\s*(.*)$",
    re.I,
)
CONFIRM_MEMORY = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?confirm\s+memory\s+([a-z0-9_-]+)\W*$",
    re.I,
)
CORRECT_EXPLICIT = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?remember\s+this\s+correction[:\s-]*(.+)$",
    re.I | re.S,
)
FEEDBACK_CONFIRM = re.compile(
    r"^\W*(?:yes[,.\s]+)?(?:remember\s+(?:that|this)\s+correction|save\s+that\s+correction|approve\s+feedback(?:\s+([a-z0-9_-]+))?)\W*$",
    re.I,
)
FEEDBACK_LIST = re.compile(r"^\W*(?:show|list)\s+(?:my\s+)?feedback\W*$", re.I)

# Phase 11 & 14 filesystem & action confirmation commands
CONFIRM_ACTION = re.compile(r"^\W*(?:confirm|approve)(?:\s+action)?(?:\s+([a-z0-9]+))?\W*$", re.I)
REJECT_ACTION = re.compile(r"^\W*(?:reject|deny|abort)\s+action(?:\s+([a-z0-9]+))?\W*$", re.I)
SEARCH_FILES = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:search\s+(?:local\s+)?files(?:\s+for)?|find\s+file(?:s)?(?:\s+named|\s+for)?)\s+(.+)$",
    re.I,
)
READ_DOC = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?read\s+(?:file|document)\s+(.+)$", re.I)
OPEN_FILE = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?open\s+(?:file|document)\s+(.+)$", re.I)
OPEN_APP = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:open|launch)\s+(?:app(?:lication)?\s+)?([a-z0-9_ .-]+?)\W*$", re.I)
OPEN_EXPLICIT_APP = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:open|launch)\s+app(?:lication)?\s+([a-z0-9_ .-]+?)\W*$", re.I)
INSPECT_PROJECT = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:inspect|analyze)\s+(?:my\s+|the\s+)?(?:project|codebase)(?:\s+at\s+(.+))?\W*$", re.I)
LIST_WINDOWS = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:list|show)\s+(?:open\s+)?windows\W*$", re.I)
ACTIVE_WINDOW = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:what\s+is\s+(?:the\s+)?(?:active|current)\s+window|active\s+window|current\s+window)\W*$",
    re.I,
)
FOCUS_WINDOW = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:focus|switch\s+to|activate)\s+window\s+(.+)$", re.I)
MEDIA_CMD = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:(play|pause|next|previous|prev|mute)\s+(?:media|music|audio|track|volume)|(volume\s+up|volume\s+down)|media\s+(play|pause|play_pause|next|previous|mute))\W*$",
    re.I,
)
TAKE_SCREENSHOT = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:take\s+(?:a\s+)?screenshot|capture\s+screen)(?:\s+as\s+([a-z0-9_.-]+))?\W*$",
    re.I,
)
OPEN_URL_CMD = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?open\s+(?:url|website|link)\s+([a-z][a-z0-9+.-]*://\S+|\S+\.\S+)\W*$",
    re.I,
)
SYSTEM_HEALTH_CMD = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:show|check|get|what\s+is)?\s*(?:my\s+|the\s+)?(?:system\s+(?:status|health|resources|metrics)|cpu\s+and\s+ram|gpu\s+(?:status|usage)|disk\s+space)\W*$",
    re.I,
)
PROACTIVE_CMD = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:show|list|any)?\s*proactive\s+suggestions\W*$",
    re.I,
)
PERSONALITY_CMD = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:switch|change|set)\s+(?:your\s+)?(?:personality|tone|mode|style)(?:\s+preset)?\s+(?:to\s+)?(professional|friendly|concise|technical|detailed|custom)\W*$",
    re.I,
)

# Phase 10 learning & training lifecycle commands
KNOWLEDGE_SYNC = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:update|sync)\s+(?:your\s+)?knowledge(?:\s+(?:from|about|on)\s+(.+))?\W*$|^\W*learn\s+the\s+latest\s+(?:information\s+)?(?:about|on)\s+(.+)$",
    re.I,
)
LEARNING_STATUS = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:what\s+have\s+you\s+learned\s+recently|learning\s+status|knowledge\s+status)\W*$",
    re.I,
)
PREPARE_DATASET = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:prepare|build)\s+(?:the\s+)?(?:training\s+)?dataset(?:\s+from\s+approved\s+feedback)?\W*$",
    re.I,
)
TRAIN_REQUEST = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:train\s+on\s+approved\s+feedback|start\s+training|prepare\s+fine[- ]?tuning)\W*$",
    re.I,
)
TRAINING_CONTROL = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(pause|resume|cancel|status)\s+training(?:\s+([a-z0-9_-]+))?\W*$|^\W*training\s+status\W*$",
    re.I,
)
EVAL_CMD = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:evaluate|run\s+evaluation\s+on)\s+(?:the\s+)?(?:candidate|active|baseline)?\s*model\W*$", re.I)
PROMOTE_CMD = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?promote\s+(?:the\s+)?(?:candidate\s+)?model(?:\s+([a-z0-9_-]+))?\W*$", re.I)
ROLLBACK_CMD = re.compile(r"^\W*(?:hey\s+)?(?:jarvis\W+)?roll\s*back(?:\s+to(?:\s+the)?\s+previous)?\s+model(?:\s+([a-z0-9_-]+))?\W*$", re.I)

# Stage B: Command history & continuity intents
REPEAT_LAST_CMD = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:repeat|re-?run|run|execute|do)\s+(?:the\s+|my\s+)?(?:last|previous)\s+(?:command|request|instruction)(?:\s+again)?\W*$",
    re.I,
)
SHOW_RECENT_TASKS = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:show|list|view)\s+(?:the\s+|my\s+)?recent\s+(?:tasks?|commands?|history|requests?)\W*$",
    re.I,
)
SHOW_FAILED_TASKS = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:show|list|view)\s+(?:the\s+|my\s+)?failed\s+(?:tasks?|commands?|requests?)\W*$",
    re.I,
)
LAST_REQUEST_STATUS = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:what\s+happened\s+to\s+(?:my\s+|the\s+)?(?:last|previous)\s+(?:request|command|task)|(?:status|outcome)\s+of\s+(?:my\s+|the\s+)?(?:last|previous)\s+(?:request|command|task))\W*$",
    re.I,
)
CONTINUE_TASK = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?(?:continue|resume)\s+(?:the\s+|my\s+)?(?:previous|last|paused|current)\s+(?:task|command|work|request)\W*$",
    re.I,
)
CANCEL_CURRENT_TASK = re.compile(
    r"^\W*(?:hey\s+)?(?:jarvis\W+)?(?:please\s+)?cancel\s+(?:the\s+|my\s+)?(?:current|active|running|pending)\s+task\W*$",
    re.I,
)


def _res(name: str, reply: str, *, nodes=None, note=None, paths=None, ui=None, ok=True, speak=True, **extra) -> dict:
    return {
        "name": name,
        "reply": reply,
        "nodes": nodes or [],
        "note": note,
        "paths": paths or ([note] if note else []),
        "ui": ui,
        "ok": ok,
        "speak": speak,
        **extra,
    }


async def route_intent(a, text: str) -> dict | None:
    """Central intent router executed before calling the LLM. Returns None when a turn should go to the LLM."""
    text = (text or "").strip()
    if not text:
        return None

    # Allow genuine web queries to flow to stream_turn when online; refuse honestly in Local Mode
    if WEB_EXPLICIT.match(text):
        if not (a.settings.internet.enabled and audit.is_online_enabled()):
            return _res(
                "web_offline",
                "Jarvis is in Local Mode (internet access is disabled). Enable Online Assisted Mode to search the web.",
                ok=False,
            )
        return None

    if STOP.match(text) or is_stop_typo(text):
        a.speaker.stop()
        if hasattr(a, "actions"):
            a.actions.tasks.cancel_active()
        if hasattr(a, "orchestrator"):
            a.orchestrator.cancel_active_runs(reason="User stop command")
        return _res("stop", "Stopped.", speak=False)

    if PAUSE_VOICE.match(text):
        a.voice.pause()
        return _res("pause_voice", "Voice listening paused.", ui={"voice": "paused"})

    if RESUME_VOICE.match(text):
        a.voice.resume()
        return _res("resume_voice", "Resuming voice listening.", ui={"voice": "listening"})

    # Stage B: Command Continuity & Task Lifecycle Controls
    if CANCEL_CURRENT_TASK.match(text):
        a.speaker.stop()
        cancelled_ids: list[str] = []
        if hasattr(a, "actions"):
            a.actions.tasks.cancel_active()
        if hasattr(a, "orchestrator"):
            a.orchestrator.cancel_active_runs(reason="Cancelled by owner command")
            all_tasks = await a.orchestrator.tasks.list_tasks(limit=25)
            for t in all_tasks:
                if t["status"] in ("queued", "planning", "running", "paused", "waiting_quota", "awaiting_permission", "awaiting_approval"):
                    await a.orchestrator.tasks.cancel_task(t["task_id"], reason="Cancelled by owner command")
                    await a.orchestrator.fs_access.consume_task_once_permissions(t["task_id"])
                    if hasattr(a.orchestrator, "command_history"):
                        await a.orchestrator.command_history.update_by_task(t["task_id"], status="cancelled", error_info="Cancelled by owner command")
                    cancelled_ids.append(t["task_id"])
        reply = (
            f"Cancelled {len(cancelled_ids)} active/pending task(s): {', '.join(cancelled_ids[:4])}."
            if cancelled_ids
            else "There is no active or pending task running right now."
        )
        return _res("cancel_task", reply, cancelled_task_ids=cancelled_ids, replayable=False, ui={"open_drawer": "tasks"})

    if REPEAT_LAST_CMD.match(text):
        if getattr(a, "_in_replay", False):
            return _res("repeat_last_command", "Refusing nested command replay loop.", ok=False, replayable=False)
        last = await a.orchestrator.command_history.get_last_replayable() if hasattr(a, "orchestrator") and hasattr(a.orchestrator, "command_history") else None
        if not last:
            return _res("repeat_last_command", "No previous replayable command was found in history.", ok=False, replayable=False)
        await a.orchestrator.command_history.increment_replay(last["id"])
        return _res(
            "repeat_last_command",
            f"Replaying last command: {last['raw_text']}",
            replay_command=last,
            replay_text=last["raw_text"],
            replayable=False,
        )

    if SHOW_RECENT_TASKS.match(text):
        tasks = await a.orchestrator.tasks.list_tasks(limit=10) if hasattr(a, "orchestrator") else []
        cmds = await a.orchestrator.command_history.list_recent(limit=10) if hasattr(a, "orchestrator") and hasattr(a.orchestrator, "command_history") else []
        if not tasks and not cmds:
            return _res("show_recent_tasks", "No recent tasks or commands have been recorded yet.", tasks=[], commands=[], replayable=False, ui={"open_drawer": "tasks"})
        lines = [f"- [{c['status']}] `{c['raw_text'][:80]}` ({c['task_category']})" for c in cmds[:6]]
        if not lines:
            lines = [f"- [{t['status']}] `{t['user_request'][:80]}` ({t['current_step']})" for t in tasks[:6]]
        return _res(
            "show_recent_tasks",
            f"Recent commands/tasks ({len(cmds) or len(tasks)}):\n" + "\n".join(lines),
            tasks=tasks,
            commands=cmds,
            replayable=False,
            ui={"open_drawer": "tasks"},
        )

    if SHOW_FAILED_TASKS.match(text):
        tasks = await a.orchestrator.tasks.list_tasks(limit=30) if hasattr(a, "orchestrator") else []
        failed_tasks = [t for t in tasks if t["status"] in ("failed", "cancelled", "unverified")]
        cmds = await a.orchestrator.command_history.list_recent(limit=15, status="failed") if hasattr(a, "orchestrator") and hasattr(a.orchestrator, "command_history") else []
        if not failed_tasks and not cmds:
            return _res("show_failed_tasks", "There are no failed tasks or commands in recent history.", tasks=[], commands=[], replayable=False, ui={"open_drawer": "tasks"})
        lines = [f"- [{t['status']}] `{t['user_request'][:70]}` — {t.get('error_info') or t.get('current_step') or 'no detail'}" for t in failed_tasks[:6]]
        for c in cmds[:4]:
            if c.get("task_id") and any(t["task_id"] == c["task_id"] for t in failed_tasks):
                continue
            lines.append(f"- [{c['status']}] `{c['raw_text'][:70]}` — {c.get('error_info') or 'failed'}")
        return _res(
            "show_failed_tasks",
            f"Failed/interrupted tasks ({len(lines)}):\n" + "\n".join(lines),
            tasks=failed_tasks,
            commands=cmds,
            replayable=False,
            ui={"open_drawer": "tasks"},
        )

    if LAST_REQUEST_STATUS.match(text):
        latest_cmd = await a.orchestrator.command_history.get_latest() if hasattr(a, "orchestrator") and hasattr(a.orchestrator, "command_history") else None
        tasks = await a.orchestrator.tasks.list_tasks(limit=5) if hasattr(a, "orchestrator") else []
        latest_task = tasks[0] if tasks else None
        if not latest_cmd and not latest_task:
            return _res("last_request_status", "No previous requests have been recorded yet.", replayable=False)
        req_text = (latest_cmd or {}).get("raw_text") or (latest_task or {}).get("user_request") or ""
        status = (latest_task or {}).get("status") or (latest_cmd or {}).get("status") or "unknown"
        step = (latest_task or {}).get("current_step") or ""
        err = (latest_task or {}).get("error_info") or (latest_cmd or {}).get("error_info") or ""
        summary = (latest_task or {}).get("result") or (latest_cmd or {}).get("result_summary") or ""
        detail = err or step or summary[:180]
        reply = f"Last request (`{req_text[:90]}`) has status `{status}`" + (f": {detail}" if detail else ".")
        return _res(
            "last_request_status",
            reply,
            task=latest_task,
            command_record=latest_cmd,
            replayable=False,
            ui={"open_drawer": "tasks"},
        )

    if CONTINUE_TASK.match(text):
        tasks = await a.orchestrator.tasks.list_tasks(limit=15) if hasattr(a, "orchestrator") else []
        pending_fs = await a.orchestrator.fs_access.list_requests(status="pending") if hasattr(a, "orchestrator") else []
        pending_ap = await asyncio.to_thread(a.orchestrator.approvals.list_pending) if hasattr(a, "orchestrator") else []
        target_task = next((t for t in tasks if t["status"] in ("awaiting_permission", "awaiting_approval", "paused", "waiting_quota", "queued", "failed", "cancelled")), None)
        if target_task is None:
            latest = tasks[0] if tasks else None
            msg = (
                f"Your latest task (`{latest['user_request'][:80]}`) is already `{latest['status']}`. Say 'repeat last command' to run it again."
                if latest
                else "There is no previous task to continue."
            )
            return _res("continue_task", msg, replayable=False, ok=False)
        t_status = target_task["status"]
        if t_status == "awaiting_permission":
            req = next((r for r in pending_fs if r.get("task_id") == target_task["task_id"]), pending_fs[0] if pending_fs else None)
            path_txt = req["canonical_path"] if req else target_task.get("current_step", "target folder")
            return _res(
                "continue_task",
                f"Previous task (`{target_task['user_request'][:70]}`) is paused waiting for filesystem permission on `{path_txt}`. Approve or deny the inline permission card to continue.",
                task=target_task,
                permission_request=req,
                replayable=False,
                ok=False,
                ui={"open_drawer": "tasks"},
            )
        if t_status == "awaiting_approval":
            ap = pending_ap[0] if pending_ap else None
            tok = ap["token"] if ap else "pending"
            return _res(
                "continue_task",
                f"Previous task (`{target_task['user_request'][:70]}`) is waiting for explicit owner approval (`{tok}`).",
                task=target_task,
                approval=ap,
                replayable=False,
                ok=False,
                ui={"open_drawer": "approvals"},
            )
        if target_task.get("worker_kind") == "deep_research" and t_status in ("paused", "waiting_quota", "queued"):
            updated = await a.orchestrator.tasks.update_task(
                target_task["task_id"],
                status="queued",
                current_step="Resumed by owner command",
                clear_not_before=True,
                clear_claim=True,
            )
            a.orchestrator.task_runner.wake()
            return _res("continue_task", f"Resumed background task `{target_task['task_id']}`.", task=updated, replayable=False)
        # Checkpointed multi-step or replayable incomplete task: let _stream_turn_unlocked execute cleanly with fresh permissions!
        cp = target_task.get("checkpoint_json") or target_task.get("checkpoint") or {}
        if cp.get("multistep_plan"):
            return _res(
                "continue_task",
                f"Continuing checkpointed task `{target_task['user_request'][:80]}`.",
                resume_task=target_task,
                replay_text=target_task["user_request"],
                replayable=False,
            )
        return _res(
            "continue_task",
            f"Re-running previous incomplete task `{target_task['user_request'][:80]}`.",
            replay_text=target_task["user_request"],
            replayable=False,
        )

    # Voice listing / switching / speech rate (must precede SWITCH_MODEL)
    if VOICE_LIST.match(text):
        voices = a.tts.list_voices()
        if not voices:
            return _res("list_voices", "No Piper voices found in models/piper.", ok=False)
        names = ", ".join(f"{v['name']} ({v['language']})" for v in voices)
        return _res("list_voices", f"Installed voices ({len(voices)}): {names}. Active: {a.settings.tts.voice}.", voices=voices)

    if m := VOICE_SWITCH.match(text):
        want = m.group(1).strip(" .?!\"'")
        try:
            switched = a.tts.switch_voice(want)
            a.settings.tts.voice = switched["name"]
            a.bus.publish({"type": "voice_changed", **switched})
            return _res(
                "switch_voice",
                f"Voice switched to {switched.get('speaker') or switched['name']} ({switched['name']}, {switched['language']}).",
                ui={"voice": switched["name"]},
                voice=switched,
            )
        except Exception as e:
            return _res("switch_voice", f"Could not switch voice to {want}: {e}", ok=False)

    if m := VOICE_RATE.match(text):
        word, num = m.group(1), m.group(2)
        if num:
            new_speed = float(num)
        elif (word or "").lower() == "faster":
            new_speed = min(1.8, round(a.settings.tts.speed + 0.15, 2))
        else:
            new_speed = max(0.6, round(a.settings.tts.speed - 0.15, 2))
        try:
            applied = a.tts.set_rate(new_speed)
            return _res("set_voice_rate", f"Speech speed set to {applied:.2f}x.", ui={"tts_speed": applied})
        except Exception as e:
            return _res("set_voice_rate", f"Invalid speech speed: {e}", ok=False)

    from .model_chat import handle_chat as _model_chat

    if (mres := await _model_chat(a, text)) is not None:
        return _res("model_catalog", mres["reply"], ok=mres["ok"], ui=mres["ui"])

    if WHICH_MODEL.match(text):
        return _res(
            "which_model",
            f"Active model is {a.brain.spec.model} ({a.brain.current}, provider: {a.brain.provider}).",
        )

    if m := PERSONALITY_CMD.match(text):
        preset = m.group(1).strip().lower()
        a.settings.preferences.personality_preset = preset
        return _res(
            "set_personality",
            f"Switched response personality preset to '{preset}'. Safety, honesty, and approval boundaries remain enforced.",
            personality_preset=preset,
            ui={"open_drawer": "settings"},
        )

    if m := SWITCH_MODEL.match(text):
        want = m.group(1).strip(" .?!\"'")
        # Clarification if user was completely ambiguous
        if want.lower() in ("model", "models", "another", "a"):
            opts = ", ".join(f"{k} ({b.model})" for k, b in a.settings.brains.items())
            return _res("clarify_model", f"Which model would you like me to switch to: {opts}?", ok=False)
        key = await a.brain.resolve_choice(want)
        if not key:
            return _res("switch", f"I have no model called {want}.", ok=False)
        try:
            res = await a.brain.switch(key)
            a.warm_status["brain"] = True
            mdl = res.get("model") or a.brain.spec.model
            return _res("switch", f"Switched to {a.brain.current} ({mdl}).", ui={"brain": a.brain.current})
        except BrainError as e:
            return _res("switch", str(e), ok=False)

    # Feedback & corrections (check before generic action confirm)
    if m := CORRECT_EXPLICIT.match(text):
        corr = m.group(1).strip()
        if hasattr(a, "feedback") and corr:
            prev_u = next((h["content"] for h in reversed(a.history.items) if h["role"] == "user"), "")
            prev_a = next((h["content"] for h in reversed(a.history.items) if h["role"] == "assistant"), "")
            try:
                rec = a.feedback.record(
                    input_text=prev_u or text,
                    previous_response=prev_a,
                    corrected_response=corr,
                    category="correction",
                    source="confirmed_correction",
                    approved=True,
                )
                return _res("feedback_record", f"Saved and approved correction [{rec.id}] for topic '{rec.topic}'.", feedback=rec.to_dict())
            except ValueError as e:
                return _res("feedback_record", str(e), ok=False)

    if m := FEEDBACK_CONFIRM.match(text):
        if hasattr(a, "feedback"):
            fid = m.group(1)
            if not fid:
                pend = a.feedback.list_records(status="pending", limit=1)
                fid = pend[0].id if pend else ""
            if not fid:
                return _res("feedback_confirm", "There is no pending correction waiting for approval.", ok=False)
            try:
                rec = a.feedback.approve(fid)
                return _res("feedback_confirm", f"Approved feedback lesson [{rec.id}] for future reference.", feedback=rec.to_dict())
            except Exception as e:
                return _res("feedback_confirm", f"Could not approve feedback: {e}", ok=False)

    if m := CONFIRM_MEMORY.match(text):
        if hasattr(a, "memory_store"):
            rec_id = m.group(1).strip()
            try:
                rec = a.memory_store.confirm_pending(rec_id, approved=True)
                return _res("confirm_memory", f"Confirmed memory {rec.id}: {rec.text}", memory=rec.to_dict())
            except Exception as e:
                return _res("confirm_memory", f"Could not confirm memory {rec_id}: {e}", ok=False)

    # Action confirmation / rejection commands
    if m := CONFIRM_ACTION.match(text):
        if hasattr(a, "actions"):
            tok = (m.group(1) or "").strip()
            pending = a.actions.pending_approvals()
            if not tok and len(pending) == 1:
                tok = pending[0]["token"]
            if not tok:
                if len(pending) > 1:
                    list_str = ", ".join(f"{p['token']} ({p['action']})" for p in pending[:4])
                    return _res("confirm_action", f"Multiple actions await approval: {list_str}. Please specify which token.", ok=False)
                return _res("confirm_action", "There are no pending actions awaiting confirmation.", ok=False)
            try:
                out = a.actions.confirm(tok)
                return _res("confirm_action", f"Confirmed and executed action {tok}.", action_result=out)
            except Exception as e:
                return _res("confirm_action", f"Action confirmation failed: {e}", ok=False)

    if m := REJECT_ACTION.match(text):
        if hasattr(a, "actions"):
            tok = (m.group(1) or "").strip()
            pending = a.actions.pending_approvals()
            if not tok and len(pending) == 1:
                tok = pending[0]["token"]
            try:
                out = a.actions.reject(tok)
                return _res("reject_action", f"Cancelled pending action {tok}.", action_result=out)
            except Exception as e:
                return _res("reject_action", f"Could not cancel action: {e}", ok=False)

    if FEEDBACK_LIST.match(text) and hasattr(a, "feedback"):
        s = a.feedback.summary()
        bs = s.get("by_status") or {}
        return _res(
            "feedback_list",
            f"Feedback store: {s['total']} total ({bs.get('approved', 0)} approved, {bs.get('pending', 0)} pending, {bs.get('rejected', 0)} rejected).",
            feedback_summary=s,
        )

    # Note count / show notes / find notes (must stay exact for compatibility)
    if COUNT_NOTES.search(text):
        return _res("count", f"I have {len(a.store.all())} saved notes in memory.")

    if SHOW_NOTES.match(text):
        a.store.refresh()
        return _res("show_notes", f"Showing all {len(a.store.all())} notes.", ui={"open": "notes"})

    if m := FIND_NOTES.match(text):
        q = (m.group(1) or m.group(2) or "").strip(" .?!")
        if not q:
            return _res("find", "Search ready.", ui={"focus": "search"})
        a.store.refresh()
        hits = [(n, s) for n, s in search(a.store.all(), q, 5) if s >= 1.5]
        if not hits:
            return _res("find", f"I found nothing about {q}.", ok=False)
        titles = ", ".join(n.title for n, _ in hits)
        return _res("find", f"Found {len(hits)}: {titles}.", nodes=[n.id for n, _ in hits], paths=[n.path for n, _ in hits], ui={"search": q})

    # Memory save / update / forget / recall / history search
    if m := REMEMBER.match(text):
        body = m.group(1).strip()
        if not body:
            return _res("remember", "What should I remember? Please share the fact or preference.", ok=False)
        try:
            res = a.store.add(body)
            mem_rec = None
            if hasattr(a, "memory_store"):
                cat, top = infer_memory_category(body)
                mem_rec, _ = a.memory_store.add(
                    body,
                    category=cat,
                    topic=top,
                    note_path=res.path,
                    confirmed=True,
                )
            return _res(
                "remember",
                f"Noted as {res.title}.",
                nodes=[res.id],
                note=res.path,
                memory=mem_rec.to_dict() if mem_rec else None,
            )
        except Exception as e:
            return _res("remember", f"I could not save that note: {e}", ok=False)

    if m := UPDATE_MEMORY.match(text):
        if hasattr(a, "memory_store"):
            rec_id, new_txt = m.group(1).strip(), m.group(2).strip()
            try:
                rec = a.memory_store.update(rec_id, new_txt)
                return _res("update_memory", f"Updated memory {rec.id}.", memory=rec.to_dict())
            except Exception as e:
                return _res("update_memory", f"Could not update memory {rec_id}: {e}", ok=False)

    if (m := FORGET_MEMORY.match(text)) and not _CONTEXT_FORGET.search(text):
        if hasattr(a, "memory_store"):
            q = m.group(1).strip(" .?!")
            # Try exact id first
            try:
                rem = a.memory_store.delete(q)
                return _res("forget_memory", f"Removed memory {rem.id} ({rem.text[:60]}).")
            except KeyError:
                hits = a.memory_store.search(q, limit=5)
                if not hits:
                    return _res("forget_memory", f"No personal memory matched '{q}'.", ok=False)
                if len(hits) == 1:
                    rem = a.memory_store.delete(hits[0][0].id)
                    return _res("forget_memory", f"Forgot memory: {rem.text[:80]}.")
                choices = ", ".join(f"{r.id}: {r.text[:35]}" for r, _ in hits[:3])
                return _res("forget_memory", f"Multiple memories matched '{q}'. Say 'forget memory <id>' for: {choices}.", ok=False)

    if m := RECALL_MEMORY.match(text) or RECALL_PREFERENCE.match(text):
        if hasattr(a, "memory_store"):
            q = m.group(1).strip(" .?!")
            mem_hits = a.memory_store.search(q, limit=4)
            hist_hits = a.history.search_disk(q, limit=3)
            if not mem_hits and not hist_hits:
                return _res("recall_memory", f"I found no stored personal memories or past turns about '{q}'.", ok=False)
            parts = []
            if mem_hits:
                parts.append("Personal memories: " + "; ".join(f"[{r.category}] {r.text}" for r, _ in mem_hits))
            if hist_hits:
                parts.append("Past conversation: " + "; ".join(h["content"][:120] for h in hist_hits))
            return _res("recall_memory", " ".join(parts))

    if m := SEARCH_HISTORY.match(text):
        q = m.group(1).strip(" .?!")
        hits = a.history.search_disk(q, limit=5)
        if not hits:
            return _res("search_history", f"No past conversation turns matched '{q}'.", ok=False)
        snippets = " | ".join(f"{h['role']}: {h['content'][:100]}" for h in hits[:3])
        return _res("search_history", f"Found {len(hits)} past turns: {snippets}", history_hits=hits)

    # Filesystem & action execution commands
    # Host directories / project folders / explicit OS paths route through DecisionEngine + P10.20 FilesystemAccessManager
    _host_fs_cue = bool(
        re.search(
            r"(?:[A-Za-z]:\\|\\\\[^\s]+|\b(?:documents|downloads|desktop|pictures|pdf\s+documents?|folder|directory|project|projects|workspace)\b|\.\.)",
            text or "",
            re.I,
        )
    )
    if not _host_fs_cue:
        if m := SEARCH_FILES.match(text):
            if hasattr(a, "filesystem"):
                q = m.group(1).strip(" .?!\"'")
                try:
                    matches = a.filesystem.search(q, limit=10)
                    if not matches:
                        return _res("search_files", f"No files or folders matched '{q}' inside allowed directories.", ok=False)
                    listed = ", ".join(x["name"] for x in matches[:5])
                    return _res("search_files", f"Found {len(matches)} matching files: {listed}.", files=matches)
                except FilesystemPermissionError as e:
                    return _res("search_files", str(e), ok=False)

        if m := READ_DOC.match(text):
            if hasattr(a, "filesystem"):
                target = m.group(1).strip(" .?!\"'")
                try:
                    doc = a.filesystem.read_document(target, max_chars=2500)
                    preview = doc["content"][:600].replace("\n", " ").strip()
                    return _res("read_file", f"{doc['path']} ({doc['chars']} chars): {preview}", document=doc)
                except Exception as e:
                    return _res("read_file", f"Could not read {target}: {e}", ok=False)

        if m := OPEN_FILE.match(text):
            if hasattr(a, "actions"):
                target = m.group(1).strip(" .?!\"'")
                try:
                    out = a.actions.request_or_execute("open_file", {"path": target})
                    if out.get("confirmation_required"):
                        return _res(
                            "open_file",
                            f"Approval required ({out['token']}): {out.get('description', target)}. Say 'confirm action {out['token']}' to proceed.",
                            action_result=out,
                            ui={"open_drawer": "approvals"},
                        )
                    return _res("open_file", f"Opened {(out.get('result') or {}).get('opened', target)}.", action_result=out)
                except Exception as e:
                    return _res("open_file", f"Could not open {target}: {e}", ok=False)

    if hasattr(a, "actions"):
        if LIST_WINDOWS.match(text):
            try:
                out = a.actions.request_or_execute("list_windows", {})
                res_data = out.get("result") or {}
                wins = res_data.get("windows", [])
                titles = ", ".join(w.get("title", "") for w in wins[:6]) or "none"
                return _res("list_windows", f"Found {len(wins)} open windows: {titles}.", action_result=out, ui={"open_drawer": "desktop"})
            except Exception as e:
                return _res("list_windows", f"Could not list windows: {e}", ok=False)

        if ACTIVE_WINDOW.match(text):
            try:
                out = a.actions.request_or_execute("get_active_window", {})
                res_data = out.get("result") or {}
                title = res_data.get("title") or "unknown"
                return _res("get_active_window", f"Active window: {title}.", action_result=out)
            except Exception as e:
                return _res("get_active_window", f"Could not read active window: {e}", ok=False)

        if m := FOCUS_WINDOW.match(text):
            title = m.group(1).strip(" .?!\"'")
            try:
                out = a.actions.request_or_execute("focus_window", {"title": title})
                if out.get("confirmation_required"):
                    return _res(
                        "focus_window",
                        f"Approval required ({out['token']}): focus window '{title}'.",
                        action_result=out,
                        ui={"open_drawer": "approvals"},
                    )
                res_data = out.get("result") or {}
                focused_title = (res_data.get("focused") or {}).get("title", title)
                return _res("focus_window", f"Focused window '{focused_title}'.", action_result=out)
            except Exception as e:
                return _res("focus_window", f"Could not focus window '{title}': {e}", ok=False)

        if m := MEDIA_CMD.match(text):
            raw_cmd = (m.group(1) or m.group(2) or m.group(3) or "").strip().lower().replace(" ", "_")
            cmd_map = {"prev": "previous", "play": "play_pause", "pause": "play_pause"}
            cmd = cmd_map.get(raw_cmd, raw_cmd)
            try:
                out = a.actions.request_or_execute("media_control", {"command": cmd})
                if out.get("confirmation_required"):
                    return _res(
                        "media_control",
                        f"Approval required ({out['token']}): media_control({cmd}).",
                        action_result=out,
                        ui={"open_drawer": "approvals"},
                    )
                return _res("media_control", f"Dispatched media control: {cmd}.", action_result=out)
            except Exception as e:
                return _res("media_control", f"Media control failed: {e}", ok=False)

        if m := TAKE_SCREENSHOT.match(text):
            fname = m.group(1).strip() if m.group(1) else None
            args = {"filename": fname} if fname else {}
            try:
                out = a.actions.request_or_execute("take_screenshot", args)
                if out.get("confirmation_required"):
                    return _res(
                        "take_screenshot",
                        f"Approval required ({out['token']}): take_screenshot.",
                        action_result=out,
                        ui={"open_drawer": "approvals"},
                    )
                res_data = out.get("result") or {}
                return _res("take_screenshot", f"Saved screenshot to {res_data.get('path', 'captures')}.", action_result=out)
            except Exception as e:
                return _res("take_screenshot", f"Screenshot failed: {e}", ok=False)

        if m := OPEN_URL_CMD.match(text):
            target_url = m.group(1).strip().rstrip(".,;?!\"'")
            if "://" not in target_url:
                target_url = "https://" + target_url
            try:
                out = a.actions.request_or_execute("open_url", {"url": target_url})
                if out.get("confirmation_required"):
                    return _res(
                        "open_url",
                        f"Approval required ({out['token']}): {out.get('description', target_url)}.",
                        action_result=out,
                        ui={"open_drawer": "approvals"},
                    )
                res_data = out.get("result") or {}
                return _res("open_url", f"Opened {res_data.get('url', target_url)}.", action_result=out)
            except Exception as e:
                return _res("open_url", f"Could not open URL: {e}", ok=False)

    if SYSTEM_HEALTH_CMD.match(text):
        from . import sysinfo

        snap = await asyncio.to_thread(sysinfo.snapshot, a.settings.monitoring, include_processes=False)
        ram_i = snap.get("ram") or {}
        gpu_i = snap.get("gpu")
        cpu_s = f"{snap['cpu_pct']}%" if snap.get("cpu_pct") is not None else "measuring"
        gpu_s = f"{gpu_i['name']} ({gpu_i['used_gb']}/{gpu_i['total_gb']} GB VRAM, {gpu_i['util_pct']}%)" if isinstance(gpu_i, dict) else "unavailable"
        disks_s = ", ".join(f"{d['mount']} {d['free_gb']}GB free" for d in (snap.get("disks") or [])[:3]) or "unknown"
        alerts = snap.get("alerts") or []
        alert_s = f" Active alerts: {len(alerts)} ({'; '.join(al['message'] for al in alerts[:2])})." if alerts else " No active resource alerts."
        return _res(
            "system_health",
            f"CPU: {cpu_s}. RAM: {ram_i.get('used_gb', 0)}/{ram_i.get('total_gb', 0)} GB ({ram_i.get('used_pct', 0)}%). GPU: {gpu_s}. Disks: {disks_s}.{alert_s}",
            system=snap,
            ui={"open_drawer": "system"},
        )

    if PROACTIVE_CMD.match(text) and hasattr(a, "proactive"):
        from . import sysinfo

        snap = await asyncio.to_thread(sysinfo.snapshot, a.settings.monitoring, include_processes=False)
        recent_tasks = await a.orchestrator.tasks.list_tasks(limit=10) if hasattr(a, "orchestrator") else []
        recent_cmds = await a.orchestrator.command_history.list_recent(limit=25) if hasattr(a, "orchestrator") and hasattr(a.orchestrator, "command_history") else []
        sugs = a.proactive.collect(sys_snapshot=snap, recent_tasks=recent_tasks, recent_commands=recent_cmds)
        if not sugs:
            return _res("proactive_suggestions", "No proactive suggestions right now.", suggestions=[], ui={"open_drawer": "system"})
        summary = " | ".join(f"{s['title']}: {s['reason']}" for s in sugs[:3])
        return _res("proactive_suggestions", f"Found {len(sugs)} suggestion(s): {summary}", suggestions=sugs, ui={"open_drawer": "system"})

    if m := OPEN_EXPLICIT_APP.match(text):
        app_name = m.group(1).strip().lower()
        if hasattr(a, "actions"):
            try:
                out = a.actions.request_or_execute("open_app", {"app": app_name})
                if out.get("confirmation_required"):
                    return _res(
                        "open_app",
                        f"Approval required ({out['token']}): launch {app_name}.",
                        action_result=out,
                        ui={"open_drawer": "approvals"},
                    )
                return _res("open_app", f"Launched {app_name}.", action_result=out)
            except Exception as e:
                return _res("open_app", f"Could not launch {app_name}: {e}", ok=False)

    if m := OPEN_APP.match(text):
        app_name = m.group(1).strip().lower()
        if hasattr(a, "actions") and app_name in a.settings.actions.allowed_apps:
            try:
                out = a.actions.request_or_execute("open_app", {"app": app_name})
                if out.get("confirmation_required"):
                    return _res(
                        "open_app",
                        f"Approval required ({out['token']}): launch {app_name}.",
                        action_result=out,
                        ui={"open_drawer": "approvals"},
                    )
                return _res("open_app", f"Launched {app_name}.", action_result=out)
            except Exception as e:
                return _res("open_app", f"Could not launch {app_name}: {e}", ok=False)

    if m := INSPECT_PROJECT.match(text):
        if hasattr(a, "filesystem"):
            target = m.group(1).strip(" .?!\"'") if m.group(1) else None
            try:
                summary = a.filesystem.inspect_project(target)
                langs = ", ".join(f"{k}: {v}" for k, v in summary.get("languages", {}).items())
                return _res("inspect_project", f"Project has {summary['total_files']} files ({langs}).", project=summary)
            except Exception as e:
                return _res("inspect_project", f"Project inspection failed: {e}", ok=False)

    # Learning & training lifecycle NLP commands (Phase 10)
    if m := KNOWLEDGE_SYNC.match(text):
        if hasattr(a, "knowledge"):
            topic = (m.group(1) or m.group(2) or "").strip(" .?!") or None
            try:
                res = await a.knowledge.sync(topic_filter=topic)
                return _res(
                    "knowledge_sync",
                    f"Knowledge sync complete: checked {res.get('sources_checked', 0)} sources, "
                    f"{res.get('new_documents_found', 0)} updated, {res.get('failed_documents', 0)} failed.",
                    knowledge=res,
                    ui={"open": "learning"},
                )
            except SearchOfflineError as e:
                return _res("knowledge_sync", str(e), ok=False)
            except Exception as e:
                return _res("knowledge_sync", f"Knowledge sync failed: {e}", ok=False)

    if LEARNING_STATUS.match(text) and hasattr(a, "knowledge"):
        ov = a.learning_overview()
        kf = ov["knowledge"]
        fb = ov["feedback"]
        bs = fb.get("by_status") or {}
        reg = ov["registry"]
        return _res(
            "learning_status",
            f"Knowledge index has {kf.get('kb_chunks', 0)} chunks across {ov.get('knowledge_sources_count', 0)} sources. "
            f"Feedback has {bs.get('approved', 0)} approved / {bs.get('pending', 0)} pending lessons. "
            f"Active model version: {reg['active_version_id']} ({a.brain.spec.model}).",
            overview=ov,
            ui={"open": "learning"},
        )

    if PREPARE_DATASET.match(text) and hasattr(a, "dataset_manager"):
        try:
            prep = a.prepare_training_dataset_from_feedback()
            b = prep["build"]
            return _res(
                "prepare_dataset",
                f"Prepared dataset build {b['build_id']}: {b['train_count']} train / {b['val_count']} val / {b['test_count']} test (0 leakage).",
                prepared=prep,
                ui={"open": "learning"},
            )
        except Exception as e:
            return _res("prepare_dataset", f"Dataset preparation failed: {e}", ok=False)

    if TRAIN_REQUEST.match(text) and hasattr(a, "training_manager"):
        try:
            builds = a.dataset_builder.list_builds()
            if not builds:
                return _res("training_request", "No prepared dataset builds found. Say 'prepare training dataset' first.", ok=False)
            job = await a.request_training_job(dataset_build_id=builds[0]["build_id"])
            est = job["estimate"]
            msg = (
                f"Created training job {job['job_id']} (state: {job['state']}). "
                f"Estimated VRAM: {est['estimated_vram_gb']} GB / available {est['hardware']['total_vram_gb']} GB. "
                + (f"Blockers: {'; '.join(est['blockers'])}" if est["blockers"] else "Ready for approval in Learning Dashboard.")
            )
            return _res("training_request", msg, job=job, ui={"open": "learning"})
        except Exception as e:
            return _res("training_request", f"Could not create training job: {e}", ok=False)

    if m := TRAINING_CONTROL.match(text):
        if hasattr(a, "training_manager"):
            action = (m.group(1) or "status").lower()
            jid = m.group(2)
            jobs = a.training_manager.list_jobs()
            if not jid and jobs:
                jid = jobs[0]["job_id"]
            if action == "status" or not jid:
                if not jobs:
                    return _res("training_status", "No training jobs have been created yet.")
                j = jobs[0]
                return _res("training_status", f"Latest job {j['job_id']}: {j['state']}.", job=j, ui={"open": "learning"})
            try:
                fn = {"pause": a.training_manager.pause_job, "resume": a.training_manager.resume_job, "cancel": a.training_manager.cancel_job}[action]
                j = fn(jid)
                return _res("training_control", f"Job {jid} -> {j['state']}.", job=j)
            except Exception as e:
                return _res("training_control", f"Training {action} failed: {e}", ok=False)

    if EVAL_CMD.match(text) and hasattr(a, "evaluator"):
        try:
            rep = await a.run_model_evaluation()
            return _res(
                "evaluate_model",
                f"Evaluation {rep['eval_id']}: baseline score {rep['baseline_score']:.2f}, decision={rep['decision']}.",
                evaluation=rep,
                ui={"open": "learning"},
            )
        except Exception as e:
            return _res("evaluate_model", f"Evaluation failed: {e}", ok=False)

    if m := PROMOTE_CMD.match(text):
        if hasattr(a, "registry"):
            vid = (m.group(1) or "").strip()
            if not vid:
                cands = [v["version_id"] for v in a.registry.summary()["versions"] if v["status"] == "candidate"]
                vid = cands[0] if cands else ""
            if not vid:
                return _res("promote_model", "No candidate model version is registered.", ok=False)
            try:
                installed = await a.brain.installed()
                rec = a.registry.promote(vid, installed_ollama_models=installed)
                return _res("promote_model", f"Promoted model version {rec['version_id']}.", version=rec)
            except Exception as e:
                return _res("promote_model", f"Promotion blocked: {e}", ok=False)

    if m := ROLLBACK_CMD.match(text):
        if hasattr(a, "rollback_model"):
            target = (m.group(1) or "").strip() or None
            try:
                res = a.rollback_model(target)
                return _res("rollback_model", f"Rolled back active model to {res['active_version_id']}.", rollback=res)
            except Exception as e:
                return _res("rollback_model", f"Rollback failed: {e}", ok=False)

    # Check non-explicit user correction (ask confirmation via feedback pending state in stream_turn or route notice)
    if hasattr(a, "feedback") and is_correction_statement(text) and len(a.history.items) >= 2:
        prev_u = next((h["content"] for h in reversed(a.history.items) if h["role"] == "user"), "")
        prev_a = next((h["content"] for h in reversed(a.history.items) if h["role"] == "assistant"), "")
        try:
            rec = a.feedback.record(
                input_text=prev_u or text,
                previous_response=prev_a,
                corrected_response=text,
                category="correction",
                source="explicit_user",
                approved=False,
            )
            return _res(
                "feedback_pending",
                f"Got it — I've noted that correction as [{rec.id}]. Remember that correction for future answers? (Say 'yes, remember that correction' to approve it.)",
                feedback=rec.to_dict(),
            )
        except ValueError as e:
            return _res("feedback_pending", f"I noted your message cleanly, but refused storing sensitive credentials ({e}).", ok=False)

    return None
