"""Graphify CLI integration (0.9.80): offline structural rebuild, query/affected blast-radius & UI graph extraction."""
import json
import os
from pathlib import Path
import shutil
import subprocess


class GraphifyError(RuntimeError):
    """Raised when graphify invocation fails or graph artifacts are invalid."""


def _find_graphify(bin_name: str = "graphify") -> str:
    return shutil.which(f"{bin_name}.exe") or shutil.which(bin_name) or bin_name


def _run_graphify(project_root: Path, args: list[str], *, bin_name: str = "graphify", timeout_s: int = 120) -> subprocess.CompletedProcess:
    exe = _find_graphify(bin_name)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [exe, *args],
        cwd=str(Path(project_root).resolve()),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        shell=False,
    )


def graph_artifact_path(project_root: Path) -> Path:
    return Path(project_root).resolve() / "graphify-out" / "graph.json"


def load_graph_json(project_root: Path) -> dict:
    """Load graphify-out/graph.json normalized into UI-friendly {nodes, links, stats}."""
    p = graph_artifact_path(project_root)
    if not p.exists():
        return {"exists": False, "nodes": [], "links": [], "node_count": 0, "edge_count": 0}
    raw = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    raw_nodes = raw.get("nodes") or []
    raw_links = raw.get("links") or raw.get("edges") or []
    nodes = [
        {
            "id": str(n.get("id") or i),
            "label": str(n.get("label") or n.get("name") or n.get("id") or i),
            "group": str(n.get("community") or n.get("file_type") or n.get("type") or "code"),
            "path": str(n.get("source_file") or n.get("file") or ""),
        }
        for i, n in enumerate(raw_nodes[:1200])
    ]
    links = [
        {"source": str(e.get("source")), "target": str(e.get("target")), "relation": str(e.get("relation") or "")}
        for e in raw_links[:2500]
        if e.get("source") is not None and e.get("target") is not None
    ]
    return {
        "exists": True,
        "artifact_path": str(p),
        "node_count": len(raw_nodes),
        "edge_count": len(raw_links),
        "nodes": nodes,
        "links": links,
    }


def update_graph(project_root: Path, *, bin_name: str = "graphify", no_cluster: bool = True, timeout_s: int = 120) -> dict:
    """Re-extract codebase structure using local AST (no LLM cost when no_cluster=True)."""
    root = Path(project_root).resolve()
    args = ["update", str(root)]
    if no_cluster:
        args.append("--no-cluster")
    proc = _run_graphify(root, args, bin_name=bin_name, timeout_s=timeout_s)
    if proc.returncode != 0 and not graph_artifact_path(root).exists():
        raise GraphifyError(f"graphify update failed: {proc.stderr.strip() or proc.stdout.strip()}")
    stats = load_graph_json(root)
    return {
        "ok": True,
        "returncode": proc.returncode,
        "stdout": proc.stdout[:4000],
        "node_count": stats["node_count"],
        "edge_count": stats["edge_count"],
        "artifact_path": str(graph_artifact_path(root)),
    }


def query_graph(project_root: Path, question: str, *, budget: int = 2000, bin_name: str = "graphify") -> dict:
    gp = graph_artifact_path(project_root)
    if not gp.exists():
        update_graph(project_root, bin_name=bin_name)
    proc = _run_graphify(project_root, ["query", question, "--budget", str(budget), "--graph", str(gp)], bin_name=bin_name, timeout_s=30)
    return {"ok": proc.returncode == 0, "question": question, "output": proc.stdout.strip()[:8000], "stderr": proc.stderr.strip()[:1000]}


def affected_nodes(project_root: Path, symbol_or_file: str, *, depth: int = 2, bin_name: str = "graphify") -> dict:
    gp = graph_artifact_path(project_root)
    if not gp.exists():
        update_graph(project_root, bin_name=bin_name)
    proc = _run_graphify(project_root, ["affected", symbol_or_file, "--depth", str(depth), "--graph", str(gp)], bin_name=bin_name, timeout_s=30)
    return {"ok": proc.returncode == 0, "target": symbol_or_file, "output": proc.stdout.strip()[:8000], "stderr": proc.stderr.strip()[:1000]}


def god_nodes(project_root: Path, *, bin_name: str = "graphify") -> dict:
    gp = graph_artifact_path(project_root)
    if not gp.exists():
        return {"ok": False, "output": "", "error": "graph.json missing"}
    proc = _run_graphify(project_root, ["god-nodes", "--graph", str(gp)], bin_name=bin_name, timeout_s=20)
    return {"ok": proc.returncode == 0, "output": proc.stdout.strip()[:6000]}

