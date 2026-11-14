# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the JARVIS Windows build (onedir). Build with scripts/build-windows.ps1.

Bundles: backend, built React frontend, default config/notes, alembic migrations.
Does NOT bundle AI models (Ollama / Whisper / Piper / wake-word): see INSTALLATION.md.
All paths are resolved relative to this file - nothing is machine specific.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()  # noqa: F821  (SPECPATH is injected by PyInstaller)

datas = [
    (str(ROOT / "config.json"), "."),
    (str(ROOT / "config"), "config"),
    (str(ROOT / "notes"), "notes"),
    (str(ROOT / "viewer"), "viewer"),
    (str(ROOT / "apps" / "frontend" / "dist"), "apps/frontend/dist"),
    (str(ROOT / "jarvis" / "db" / "migrations"), "jarvis/db/migrations"),
    (str(ROOT / "jarvis" / "agents" / "builtin_agents"), "jarvis/agents/builtin_agents"),
]
binaries, hiddenimports = [], collect_submodules("jarvis")

# Packages that ship native libraries / data files PyInstaller cannot discover on its own.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "openwakeword", "piper", "sounddevice", "tokenizers",
            "av", "alembic", "langgraph", "langgraph.checkpoint", "tiktoken", "certifi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except (Exception, SystemExit) as exc:  # optional package missing in this environment: the build log shows which
        print(f"[jarvis_desktop.spec] skipped {pkg}: {exc}")

# mcp.cli needs the optional typer extra and calls sys.exit() on import: leave the CLI out of the bundle.
hiddenimports += collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
datas += collect_data_files("mcp")

hiddenimports += [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite", "aiosqlite",
]

a = Analysis(
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "jupyter", "IPython", "pytest", "torch", "tensorflow"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="jarvis",
    console=False,  # tray app; use `jarvis.exe --no-tray` from a terminal for console output
    icon=str(ROOT / "apps" / "frontend" / "public" / "favicon.ico") if (ROOT / "apps" / "frontend" / "public" / "favicon.ico").exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="jarvis")  # noqa: F821
