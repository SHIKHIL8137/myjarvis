# 05 — Frontend Audit & React Migration Strategy

> **Scope**: `viewer/index.html` (297L), `viewer/style.css` (211L), 6 custom ES modules (`viewer/js/*.js` totaling `1,359` lines), and `viewer/vendor/` (`3d-force-graph.min.js` + Orbitron `.woff2`). Verified `node --check` passes (`exit 0`) on all 6 custom `.js` modules.

---

## 1. Current Frontend Inventory & Responsibility Breakdown

| File | Lines | Responsibility | Observations & Issues |
| :--- | :--- | :--- | :--- |
| `viewer/index.html` | 297L | Full HUD layout: header telemetry pills, central chat pane + voice bar, 3D knowledge galaxy canvas, Learning Center modal (6 tabs), Workspace Drawer (4 tabs), Settings Drawer | Hardcodes static HTML skeletons for every panel/drawer; tightly couples IDs (`#l-body`, `#w-body`, `#chat`, etc.) globally across modules. |
| `viewer/style.css` | 211L | Responsive CSS Grid, glassmorphic dark sci-fi palette, animated status bars, learning/workspace grids | Single flat stylesheet with un-namespaced utility classes; difficult to scale as agent/project panels grow. |
| `viewer/js/api.js` | 56L | `api()` fetch wrapper, DOM builder `el(tag, cls, text)`, query helpers `$`/`$$`, `toast()`, simple in-memory pub/sub (`on`, `emit`) | Clean lightweight abstraction, but completely untyped (no TypeScript contracts for the 90 backend operations) and lacks request cancellation (`AbortController`) or retry policies. |
| `viewer/js/md.js` | 51L | Regex Markdown → HTML string renderer (`renderMarkdown`) written via `node.innerHTML` | Properly HTML-escapes `& < >` first and `el()` uses `textContent`, **but** `[label](url)` link handling only checks `https?://` in `validate`? Needs explicit verification/enforcement against `javascript:` URLs in any Markdown renderer replacement. |
| `viewer/js/galaxy.js` | 148L | Wraps vendored `3d-force-graph` WebGL canvas, node coloring by group, click-to-inspect note, zoom/highlight sync | Good isolated wrapper; **CRITICAL INSIGHT**: keeps GPU WebGL context stable on 4 GB GTX 1650 because canvas isn't recreated on DOM text updates. |
| `viewer/js/main.js` | 444L | Chat submission, SSE `/api/chat/stream` reader, global `/api/events` `EventSource`, voice session controls, 44-bar SVG audio level meter, STT transcript correction/translation inline actions, PTT interactions | Mixes transport (`fetch` + SSE parsing), application state (`voiceState`, `streaming`, `transcripts`), and imperative DOM mutations in one 444L file. |
| `viewer/js/panels.js` | 230L | Notes CRUD viewer, Settings drawer (brain selector, STT language/quality, TTS voice/rate, continuous/half-duplex/barge-in toggles, Internet/Provider API key manager), system meters | Monolithic form serialization/hydration logic; mutates raw DOM inputs tied to `config` keys. |
| `viewer/js/learning.js` | 538L | Largest JS file: renders 6 Learning tabs (`Overview`, `Knowledge`, `Datasets`, `Training`, `Evaluation`, `Registry`) **and** 4 Workspace tabs (`Memory`, `History`, `Files`, `Actions`), plus starts `setInterval(..., 5000)` pollers on drawer open | Cross-domain conflation (Workspace files/actions live inside `learning.js`); 5-second background polling duplicates SSE event push and triggers side-effect writes on `/api/learning/overview`. |
| `viewer/vendor/*` | 1.39MB | Local offline copies of `3d-force-graph.min.js` + `orbitron-500/700.woff2` | **Strength**: Zero external CDN dependency (`tests/test_api.py::test_viewer_has_no_cdn_or_external_urls` enforces offline safety). |

---

## 2. Structural Limitations vs Future AI OS Requirements

1. **No Typed API Contract**: With 90 existing operations—and upcoming Multi-Agent Runs, Project Switcher, Graphify Architecture Viewer, OpenCode Coding Diffs/Logs, Tool Approvals, Document Export (PDF/DOCX/PPTX/XLSX), and DR Backup Manager—maintaining raw `document.createElement` builders across un-typed vanilla JS files will rapidly become error-prone and unreadable.
2. **Imperative DOM State Sync**: Audio state (`LISTENING` / `SPEAKING` / `INTERRUPTED`), active project context, agent workflow DAG steps, and pending permission badges require reactive state synchronization across panels without manual `querySelector` DOM patching.
3. **Polling vs Streaming**: `learning.js` polls every 5 seconds instead of subscribing cleanly to typed SSE channels (`agent_events`, `training_progress`, `voice_events`, `task_updates`).
4. **Desktop Hardware Caution (GTX 1650 + 15.35 GB RAM)**: Heavy Electron shells or multiple heavy WebGL canvases would compete for GPU VRAM with Ollama/Whisper. The browser frontend must remain lightweight, offline-bundled (zero CDNs), and isolate the existing `3d-force-graph` WebGL scene so it mounts once and never leaks WebGL contexts.

---

## 3. Recommended Migration Strategy: Incremental React + Vite + TypeScript

### Target Frontend Stack (`apps/frontend` or `frontend/`)
- **Core**: **React 18+**, **TypeScript**, **Vite** (producing static offline bundles served directly by FastAPI or `vite dev` proxying `/api/*` during development).
- **State Management**: Lightweight store (`Zustand` or Context + `TanStack Query` for server cache/invalidation via SSE events).
- **UI Architecture**:
  - `src/api/`: Typed Fetch/SSE client generated or validated from FastAPI OpenAPI (`openapi-typescript`), central `AbortController` handling.
  - `src/features/hud/`: Persistent top telemetry + active project/mode/agent switcher.
  - `src/features/voice/`: Continuous voice state indicator, STT transcript diff/correction card, voice/mic/AEC controls.
  - `src/features/chat/`: Streaming assistant chat, inline citations (`Personal`, `Project`, `KB`, `Web`), tool-execution cards, generated-document download cards (`PDF`/`DOCX`/`PPTX`/`XLSX`).
  - `src/features/galaxy/`: **Memoized Canvas Island** hosting `3d-force-graph` + Graphify architecture graphs cleanly isolated via `React.memo` & `useRef` (zero re-mount on chat updates).
  - `src/features/workforce/`: Agent Workforce console (live LangGraph step stream, OpenCode task logs, Git worktree status, diff preview, interactive Permission Approval queue).
  - `src/features/memory/`: Separated views matching the 5-way memory architecture (Personal Memory, Project Knowledge/Graphify, Experience/Lessons, External KB, Training Datasets/Jobs/Registry).
  - `src/features/recovery/`: 3-2-1 Backup status, manifest inspector, restore dry-run & verification wizard.

### Safe Strangler-Fig Rollout Plan
1. Keep existing `viewer/` untouched at `/` during backend stabilization (`P0–P3`).
2. Scaffold Vite + React + TS in `frontend/`, bundling fonts & `3d-force-graph` offline inside `dist/` (preserving strict zero-CDN policy enforced by `test_viewer_has_no_cdn_or_external_urls`).
3. Mount built React bundle at `/app` during transition (`P6–P8`) with feature flags to toggle default `/`.
4. Retire `viewer/` to `legacy/viewer/` only after 100% functional parity (Voice + Chat + 3D Galaxy + Settings + Learning + Agent Workforce + Approvals) is Playwright-verified.
