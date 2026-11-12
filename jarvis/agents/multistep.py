"""Multi-Step Task Classification, Dependency Decomposition, and Checkpointed Sequential Executor."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any, Awaitable, Callable

from ..filesystem_access import FilesystemAccessManager, FilesystemSecurityError, READ_OPS
from .tasks import AssistantTaskService

TASK_CATEGORIES: tuple[str, ...] = (
    "informational",
    "filesystem_read",
    "filesystem_write",
    "desktop_action",
    "browser_web",
    "coding_project",
    "multi_step",
)

_ACTION_VERBS_RE = re.compile(
    r"\b(open|inspect|find|search|read|summarize|explain|check|list|analyze|create|write|edit|save|delete|launch|monitor|report)\b",
    re.I,
)
_SEQUENTIAL_CONNECTOR_RE = re.compile(r"\b(?:and\s+then|then|after\s+that|followed\s+by)\b|;|->|,\s*(?:inspect|find|read|explain|summarize|check|search|save|create|list)\b", re.I)


def classify_task_category(text: str, *, intent: str = "", department: str = "") -> str:
    """Classify an incoming instruction across Jarvis's 7 operational task categories."""
    s = (text or "").strip()
    low = s.lower()
    if department == "dev" or intent in ("dev_coding", "dev_debug", "dev_architecture", "security_audit"):
        return "coding_project"
    verbs = [v.lower() for v in _ACTION_VERBS_RE.findall(s)]
    has_seq_connector = bool(_SEQUENTIAL_CONNECTOR_RE.search(s))
    distinct_verbs = list(dict.fromkeys(verbs))
    if (len(distinct_verbs) >= 2 and (has_seq_connector or " and " in low)) or len(distinct_verbs) >= 3:
        return "multi_step"
    if intent in ("filesystem_ops", "document_export"):
        if re.search(r"\b(?:create|write|save|edit|modify|delete|generate|export)\b", low):
            return "filesystem_write"
        return "filesystem_read"
    if re.search(r"\b(?:open\s+app|launch|close\s+window|focus\s+window|screenshot|volume|brightness|mute)\b", low):
        return "desktop_action"
    if intent in ("web_research", "research") or re.search(r"\b(?:web|browser|online|url|website|search\s+internet)\b", low):
        return "browser_web"
    return "informational"


def _extract_focus_subdir(text: str) -> str:
    m = re.search(r"\b(?:inspect|open|check|search|in)\s+(?:the\s+)?(backend|frontend|server|client|api|src|docs|config)\b", text or "", re.I)
    return m.group(1).lower() if m else ""


def _extract_topic_keywords(text: str) -> list[str]:
    low = (text or "").lower()
    kws: list[str] = []
    if "database" in low or re.search(r"\bdb\b", low):
        kws.extend(["database", "db", "sqlalchemy", "postgres", "sqlite", "mysql", "mongo", "engine", "config", "settings"])
    elif "auth" in low or "login" in low:
        kws.extend(["auth", "login", "jwt", "session", "oauth", "security"])
    elif "route" in low or "api" in low or "endpoint" in low:
        kws.extend(["api", "routes", "router", "endpoints", "main", "app"])
    elif "config" in low or "settings" in low:
        kws.extend(["config", "settings", "pyproject", "package", "env"])
    else:
        for m in re.finditer(r"\b(?:find|search\s+for|read|for|about)\s+(?:the\s+)?([a-z0-9_.-]{3,32})", low):
            tok = m.group(1)
            if tok not in {"the", "file", "files", "folder", "backend", "frontend", "project", "connection"}:
                kws.append(tok)
    return list(dict.fromkeys(kws))[:8]


def build_multistep_plan(
    text: str,
    *,
    resolved_target: str | None = None,
    alias_name: str | None = None,
) -> list[dict[str, Any]]:
    """Decompose a compound instruction into dependency-ordered verifiable steps."""
    low = (text or "").lower()
    steps: list[dict[str, Any]] = []
    focus_sub = _extract_focus_subdir(text)
    keywords = _extract_topic_keywords(text)

    if resolved_target:
        steps.append(
            {
                "key": "step-1-structure",
                "kind": "fs_inspect",
                "title": f"Inspect project/folder structure (`{alias_name or resolved_target}`)",
                "target": resolved_target,
                "focus_subdir": focus_sub,
            }
        )
        if re.search(r"\b(?:find|search|locate|config|database|auth|api|route|test|spec|invoice|pdf|docx|report)\b", low):
            steps.append(
                {
                    "key": "step-2-search",
                    "kind": "fs_search",
                    "title": f"Locate relevant files ({', '.join(keywords[:3]) or 'target topic'})",
                    "target": resolved_target,
                    "focus_subdir": focus_sub,
                    "keywords": keywords or [focus_sub] if focus_sub else keywords,
                    "depends_on": "step-1-structure",
                }
            )
            steps.append(
                {
                    "key": "step-3-read",
                    "kind": "fs_read",
                    "title": "Read verified matching source/config files safely",
                    "target": resolved_target,
                    "keywords": keywords,
                    "depends_on": "step-2-search",
                }
            )
            prev_key = "step-3-read"
        else:
            steps.append(
                {
                    "key": "step-2-read",
                    "kind": "fs_read",
                    "title": "Read key top-level project/document files",
                    "target": resolved_target,
                    "keywords": keywords,
                    "depends_on": "step-1-structure",
                }
            )
            prev_key = "step-2-read"

        steps.append(
            {
                "key": "step-4-synthesize",
                "kind": "synthesize",
                "title": "Analyze verified evidence and answer the user's question",
                "depends_on": prev_key,
            }
        )
        return steps

    # Non-FS sequential clause breakdown (system / informational / mixed)
    raw_clauses = [c.strip() for c in re.split(r"(?:\bthen\b|;|->|,\s+(?=and\b)|,\s*)", text or "", flags=re.I) if c.strip()]
    for idx, cl in enumerate(raw_clauses[:4], 1):
        steps.append({"key": f"step-{idx}", "kind": "clause", "title": cl[:160], "clause": cl})
    steps.append({"key": f"step-{len(steps) + 1}-synthesize", "kind": "synthesize", "title": "Synthesize sequential step results"})
    return steps


class MultiStepExecutor:
    """Executes sequential dependency steps with checkpoints, timeouts, transient retries, and permission pause/resume."""

    def __init__(
        self,
        *,
        fs_access: FilesystemAccessManager,
        tasks: AssistantTaskService,
        bus: Any = None,
        per_step_timeout_s: float = 25.0,
    ):
        self.fs_access = fs_access
        self.tasks = tasks
        self.bus = bus
        self.per_step_timeout_s = float(per_step_timeout_s)

    async def execute(
        self,
        *,
        goal: str,
        steps: list[dict[str, Any]],
        task_id: str | None,
        synthesize_fn: Callable[[str, dict[str, Any], list[str], list[str]], Awaitable[str]],
        cancel_event: asyncio.Event | None = None,
        source: str = "text",
        resume_checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cp = dict(resume_checkpoint or {})
        step_outputs: dict[str, Any] = dict(cp.get("step_outputs") or {})
        collected_paths: list[str] = list(cp.get("paths") or [])
        warnings: list[str] = list(cp.get("warnings") or [])
        tools_used: list[str] = list(cp.get("tools_used") or [])
        candidate_files: list[str] = list(cp.get("candidate_files") or [])

        plan_titles = [str(s.get("title") or s.get("key")) for s in steps]
        if task_id:
            await self.tasks.update_task(
                task_id,
                status="running",
                plan=plan_titles,
                current_step=f"Starting {len(steps)}-step plan",
            )

        for step in steps:
            if cancel_event and cancel_event.is_set():
                if task_id:
                    await self.tasks.cancel_task(task_id, reason="Cancelled during multi-step workflow")
                return {"command": "multi_step", "reply": "Task cancelled before completion.", "paths": collected_paths, "interrupted": True, "ok": False}

            if task_id:
                snap = await self.tasks.get_task(task_id)
                if snap and snap["status"] == "cancelled":
                    return {"command": "multi_step", "reply": "Task cancelled by owner.", "paths": collected_paths, "interrupted": True, "ok": False}
                completed_keys = set(snap.get("completed_step_keys") or []) if snap else set()
                if step["key"] in completed_keys and step["key"] in step_outputs:
                    continue

            kind = str(step.get("kind") or "clause")
            if task_id:
                await self.tasks.update_task(task_id, status="running", current_step=step["title"][:180])

            # Bounded single transient retry on OS/Timeout failures; NEVER retry permission/security blocks
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    out = await asyncio.wait_for(
                        self._run_single_step(
                            goal=goal,
                            step=step,
                            task_id=task_id,
                            step_outputs=step_outputs,
                            candidate_files=candidate_files,
                            collected_paths=collected_paths,
                            warnings=warnings,
                            tools_used=tools_used,
                            steps=steps,
                            synthesize_fn=synthesize_fn,
                        ),
                        timeout=self.per_step_timeout_s,
                    )
                    if out.get("permission_request"):
                        if task_id:
                            await self.tasks.update_task(
                                task_id,
                                status="awaiting_permission",
                                append_permission=out["permission_request"]["request_id"],
                                checkpoint_patch={
                                    "step_outputs": step_outputs,
                                    "candidate_files": candidate_files,
                                    "paths": collected_paths,
                                    "warnings": warnings,
                                    "tools_used": tools_used,
                                },
                                current_step=f"Waiting for permission ({out['permission_request']['canonical_path']})",
                            )
                        return {
                            "command": "permission_required",
                            "reply": f"Permission required to inspect `{out['permission_request']['canonical_path']}`. Approve or deny in the inline permission card to continue all remaining steps.",
                            "permission_request": out["permission_request"],
                            "paths": [],
                            "ok": False,
                        }
                    if out.get("blocked"):
                        if task_id:
                            await self.tasks.update_task(task_id, status="failed", error_info=out["error"], current_step="Blocked by security policy")
                        return {"command": "filesystem_blocked", "reply": f"Blocked by security policy: {out['error']}", "paths": collected_paths, "ok": False}

                    step_outputs[step["key"]] = out.get("data")
                    if kind == "synthesize":
                        final_reply = str(out.get("reply") or "").strip()
                        if warnings:
                            uniq_w = list(dict.fromkeys(warnings))
                            final_reply = f"{final_reply}\n\nSafety/Inspection Notice: {'; '.join(uniq_w)}"
                        if task_id:
                            await self.tasks.record_step_completion(
                                task_id,
                                step["key"],
                                checkpoint_patch={
                                    "step_outputs": step_outputs,
                                    "candidate_files": candidate_files,
                                    "paths": collected_paths,
                                    "warnings": warnings,
                                    "tools_used": tools_used,
                                },
                                current_step="Multi-step task completed",
                            )
                            await self.tasks.update_task(
                                task_id,
                                status="completed",
                                tools_used=list(dict.fromkeys(tools_used)),
                                generated_files=list(dict.fromkeys(collected_paths)),
                                result=final_reply[:1500],
                                current_step="Multi-step task completed",
                            )
                            await self.fs_access.consume_task_once_permissions(task_id)
                        return {
                            "command": "multi_step",
                            "reply": final_reply,
                            "paths": list(dict.fromkeys(collected_paths)),
                            "tools_used": list(dict.fromkeys(tools_used)),
                            "warnings": list(dict.fromkeys(warnings)),
                            "plan": plan_titles,
                            "step_outputs": step_outputs,
                            "ok": True,
                        }

                    if task_id:
                        await self.tasks.record_step_completion(
                            task_id,
                            step["key"],
                            checkpoint_patch={
                                "step_outputs": step_outputs,
                                "candidate_files": candidate_files,
                                "paths": collected_paths,
                                "warnings": warnings,
                                "tools_used": tools_used,
                            },
                            current_step=f"Completed: {step['title'][:150]}",
                            delta_tools=1,
                        )
                    last_err = None
                    break
                except FilesystemSecurityError as exc:
                    if task_id:
                        await self.tasks.update_task(task_id, status="failed", error_info=str(exc), current_step=f"Blocked at {step['key']}")
                    return {"command": "filesystem_blocked", "reply": f"Blocked by filesystem security policy: {exc}", "paths": collected_paths, "ok": False}
                except (asyncio.TimeoutError, OSError) as exc:
                    last_err = exc
                    if attempt == 0:
                        await asyncio.sleep(0.1)
                        continue
            if last_err is not None:
                msg = f"Step '{step['title']}' failed after retry: {last_err}"
                if task_id:
                    await self.tasks.update_task(task_id, status="failed", error_info=msg, current_step=f"Failed at {step['key']}")
                    await self.fs_access.consume_task_once_permissions(task_id)
                return {"command": "multi_step", "reply": msg, "paths": collected_paths, "ok": False}

        # Fallback if plan lacked explicit synthesize step
        if task_id:
            await self.fs_access.consume_task_once_permissions(task_id)
        return {
            "command": "multi_step",
            "reply": "Completed planned steps.",
            "paths": list(dict.fromkeys(collected_paths)),
            "tools_used": list(dict.fromkeys(tools_used)),
            "ok": True,
        }

    async def _run_single_step(
        self,
        *,
        goal: str,
        step: dict[str, Any],
        task_id: str | None,
        step_outputs: dict[str, Any],
        candidate_files: list[str],
        collected_paths: list[str],
        warnings: list[str],
        tools_used: list[str],
        steps: list[dict[str, Any]],
        synthesize_fn: Callable[[str, dict[str, Any], list[str], list[str]], Awaitable[str]],
    ) -> dict[str, Any]:
        kind = str(step.get("kind") or "")
        target = str(step.get("target") or "")

        if kind == "fs_inspect":
            chk = await self.fs_access.authorize(target, "list_directory", task_id=task_id)
            if chk.blocked:
                return {"blocked": True, "error": chk.reason}
            if not chk.allowed:
                req = await self.fs_access.create_permission_request(
                    target,
                    list(READ_OPS),
                    task_id=task_id,
                    reason=f"Multi-step task requires read access to `{target}`: {goal[:120]}",
                    access_duration="once",
                    deferred_action={"kind": "multistep_resume", "goal": goal, "steps": steps, "folder": target},
                )
                return {"permission_request": req}
            inspect_target = Path(target)
            sub = str(step.get("focus_subdir") or "").strip()
            if sub and (inspect_target / sub).is_dir():
                inspect_target = inspect_target / sub
            tree = await self.fs_access.execute_get_project_structure(inspect_target, task_id=task_id, max_depth=3)
            tools_used.append("fs_get_project_structure")
            collected_paths.append(tree["path"])
            return {"data": tree}

        if kind == "fs_search":
            chk = await self.fs_access.authorize(target, "search_files", task_id=task_id)
            if chk.blocked:
                return {"blocked": True, "error": chk.reason}
            if not chk.allowed:
                req = await self.fs_access.create_permission_request(
                    target,
                    list(READ_OPS),
                    task_id=task_id,
                    reason=f"Multi-step search needs read access to `{target}`",
                    access_duration="once",
                    deferred_action={"kind": "multistep_resume", "goal": goal, "steps": steps, "folder": target},
                )
                return {"permission_request": req}
            search_root = Path(target)
            sub = str(step.get("focus_subdir") or "").strip()
            if sub and (search_root / sub).is_dir():
                search_root = search_root / sub
            keywords = [str(k) for k in (step.get("keywords") or []) if k]
            hits_map: dict[str, dict[str, Any]] = {}
            for kw in (keywords[:5] or [""]):
                found = await self.fs_access.execute_search_files(search_root, query=kw, task_id=task_id, limit=15)
                for m in found:
                    hits_map[m["path"]] = m
            # Also find common code/config files if filename match was sparse
            if len(hits_map) < 3:
                code_files = await self.fs_access.execute_find_files_by_extension(
                    search_root,
                    [".py", ".ts", ".js", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"],
                    task_id=task_id,
                    max_results=15,
                )
                for cf in code_files:
                    hits_map.setdefault(cf["path"], cf)
            ranked = sorted(hits_map.values(), key=lambda item: (0 if any(k in item["name"].lower() for k in keywords) else 1, item["name"].lower()))
            for r in ranked[:4]:
                if r["path"] not in candidate_files:
                    candidate_files.append(r["path"])
                if r["path"] not in collected_paths:
                    collected_paths.append(r["path"])
            tools_used.append("fs_search_files")
            return {"data": {"root": str(search_root), "matches": ranked[:12]}}

        if kind == "fs_read":
            keywords = [str(k).lower() for k in (step.get("keywords") or []) if k]
            targets_to_read = list(candidate_files[:3])
            if not targets_to_read and target:
                p = Path(target)
                if p.is_file():
                    targets_to_read = [str(p)]
                elif p.is_dir():
                    chk_dir = await self.fs_access.authorize(target, "list_directory", task_id=task_id)
                    if chk_dir.blocked:
                        return {"blocked": True, "error": chk_dir.reason}
                    if chk_dir.allowed:
                        listed = await self.fs_access.execute_list_directory(target, task_id=task_id, limit=12)
                        targets_to_read = [e["path"] for e in listed if not e.get("is_dir")][:3]
            read_results: list[dict[str, Any]] = []
            for fp in targets_to_read:
                chk = await self.fs_access.authorize(fp, "read_file", task_id=task_id)
                if chk.blocked:
                    warnings.append(f"Skipped protected/blocked file `{Path(fp).name}`: {chk.reason}")
                    continue
                if not chk.allowed:
                    continue
                try:
                    doc = await self.fs_access.execute_read_file(fp, task_id=task_id, max_bytes=20_000, max_lines=160)
                    tools_used.append("fs_read_file")
                    if doc["path"] not in collected_paths:
                        collected_paths.append(doc["path"])
                    if doc.get("redacted"):
                        warnings.append(f"Secrets redacted in {doc['name']}: {', '.join(doc.get('redacted_kinds') or [])}")
                    if doc.get("truncated"):
                        warnings.append(f"Truncated preview of {doc['name']} ({doc.get('total_lines')} total lines)")
                    # Prioritize files whose content actually discusses target keywords when many code files exist
                    rel_score = sum(1 for k in keywords if k in (doc.get("content") or "").lower() or k in doc["name"].lower())
                    read_results.append({"path": doc["path"], "name": doc["name"], "score": rel_score, "content": doc["content"], "sha256": doc.get("sha256", "")})
                except FilesystemSecurityError as exc:
                    warnings.append(f"Skipped `{Path(fp).name}`: {exc.reason}")
            read_results.sort(key=lambda d: -int(d.get("score") or 0))
            return {"data": read_results}

        if kind == "clause":
            return {"data": {"clause": step.get("clause") or step.get("title"), "status": "acknowledged"}}

        if kind == "synthesize":
            reply = await synthesize_fn(goal, step_outputs, collected_paths, warnings)
            return {"data": {"reply": reply}, "reply": reply}

        return {"data": {}}
