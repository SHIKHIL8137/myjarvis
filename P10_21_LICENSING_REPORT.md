# P10.21 — Licensing & Clean-Room Reuse Analysis Report

**Audit Date:** 2026-11-12  
**Upstream Inspected:** `https://github.com/FatihMakes/Mark-LVII` @ `752ff618a4e6a4bf2e675866b993071d739cfce2`  
**Jarvis Target Repo:** `d:\projects\myjarvis`

---

## 1. Upstream License Findings

Inspecting `LICENSE` (lines 1–250 of 417) in the `Mark-LVII` repository directly confirms:
- **Copyright Notice:** `MARK 56 — JARVIS Copyright (c) 2026 FatihMakes`
- **License:** **Creative Commons Attribution-NonCommercial 4.0 International (`CC BY-NC 4.0`)**
- **Key Legal Restrictions:**
  - **NonCommercial Clause:** *"Commercial use is not permitted"* — you may NOT share, copy, adapt, or distribute Mark LVII's source code, prompt text (`core/prompt.txt`), system instructions, documentation snippets, UI artwork, sounds, or assets in any product or codebase that is commercial or could ever be licensed/monetized commercially.
  - **ShareAlike / Attribution Burden & Mixing Contamination:** Even for non-commercial derivative works, incorporating `CC BY-NC 4.0` code directly into Jarvis would taint Jarvis's repository license compatibility and create ambiguous ownership across mixed files.
  - **Already Enforced in Jarvis's Own Registry Policy:** Notice that `jarvis/skills/registry.py:33` explicitly restricts allowed third-party licenses to permissive/attribution standards (`{"mit", "apache-2.0", "bsd-3-clause", "bsd-2-clause", "isc", "cc-by-4.0", "cc0-1.0"}`) and excludes `CC BY-NC 4.0`!

---

## 2. Clean-Room Implementation Boundary (What Can vs. Cannot Be Reused)

### STRICTLY PROHIBITED (Never Copy or Adapt Text/Code Directly)
- Zero Python files, functions, classes, or helper snippets from Mark LVII (`core/*`, `actions/*`, `providers/*`, `dashboard/*`, `main.py`).
- Zero prompt wording verbatim from `core/prompt.txt` or `agents/*.md` persona/instruction templates.
- Zero UI layouts, stylesheet assets, viseme math assets, icons, sounds, or README benchmarks/text.
- Keep the read-only scratch clone of Mark LVII completely isolated outside `d:\projects\myjarvis` and do not reference or import it in runtime/tests.

### SAFE TO DESIGN INDEPENDENTLY (General Software Architecture Patterns)
Software ideas, state machines, security invariants, and architectural concepts are **not copyrightable expression** when designed from scratch in Jarvis's own stack (`FastAPI`, `SQLAlchemy`, `Alembic`, `LangGraph`, `ModelGateway`, `VoiceLoop`, React/TS):
- **Checkpointed persistent task queues** with startup re-queue, retry limits, token/time budget wrap-up, and cooperative shutdown.
- **Multi-stage research pipelines** (`Plan -> Search/Fetch -> Review -> Draft -> Programmatic Quote/Citation Check`).
- **Untrusted-content boundary escaping** (`<<<UNTRUSTED_DATA>>>`) and in-code separation between web retrieval tools and private memory retrieval.
- **SHA-256 conflict-checked rollback journals** for reversible local mutations.
- **Structured tool post-condition receipts** (`done` / `unverified` / `failed`).
- **Scheduled routine engines** with catch-up grace windows and quiet-hours suppression.
- **Dynamic prompt capability flags** reflecting live subsystem availability.
- **Timed audio transport probe checks** via existing `sounddevice` library.

---

## 3. Dependency & Runtime License Compatibility Check

Checking `Mark LVII/requirements.txt` vs Jarvis's existing stack shows **no need to adopt restrictive GPL or heavy GUI/platform dependencies**:

| Dependency in Mark LVII | License / Ecosystem Risk | Verdict for Jarvis |
|---|---|---|
| `PyQt6` (6.x) | **GPL v3 / Commercial Qt License** — strong copyleft contamination risk if distributed commercially! | **Reject** — Jarvis uses permissive/MIT-compatible React + FastAPI web stack. |
| `mss`, `pillow`, `psutil`, `httpx`, `cryptography`, `fastapi`, `numpy` | Permissive (MIT / BSD / Apache-2.0 / PSF) | Already installed (`httpx`, `cryptography`, `fastapi`, `numpy`, `python-docx`, `python-pptx`, `reportlab`, `openpyxl`) or permissively available if needed later (`mss`, `pillow`, `send2trash`, `psutil`). |
| `openwakeword`, `faster-whisper`, `piper-tts` | Apache-2.0 / MIT | Already integrated locally in Jarvis (`jarvis/wakeword.py`, `jarvis/stt.py`, `jarvis/tts.py`); never use runtime `pip install` auto-installer. |
| `rapidocr_onnxruntime` / `paddleocr` / `pywinauto` / `playwright` | Apache-2.0 / BSD-3 (heavy native binaries) | Defer unless P2 vision/screen inspection explicitly proves pure `qwen2.5vl:3b` + DOM/file extraction insufficient. |
| `opencv-python` (`cv2`) | Apache-2.0 | Defer until P2 camera capture is explicitly approved & implemented under strict privacy gates. |

**Conclusion:** Jarvis can implement every valuable P0–P2 capability completely **clean-room** using its existing permissively licensed stack (`FastAPI`, `SQLAlchemy`, `LangGraph`, `httpx`, `faster-whisper`, `piper-tts`, `openwakeword`, `qwen3` / `qwen2.5vl:3b` on Ollama) without introducing any `CC BY-NC 4.0` or `GPL` risk.

