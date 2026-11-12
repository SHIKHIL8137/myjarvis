# JARVIS P10.18 — Knowledge Graph Architecture Report

## 1. Authoritative Data Model & Identity
All graph state originates exclusively from real Jarvis backend persistence and runtime orchestration inside `jarvis/graph_live.py` (`build_graph`). Nodes use deterministic typed identifiers `<type>:<key>` to guarantee idempotency across snapshots and deltas:
- **Notes**: `note:<uid>` from `NoteStore` (`notes/*.md` titles, categories, excerpts, `linked` wikilink/reference edges).
- **Personal Memory**: `memory:<id>` from `FiveWayMemoryService` and `PersonalMemoryStore` (`belongs_to` project, `supersedes` memory, `saved_as` note).
- **Experience Lessons**: `experience:<id>` (`learned_in` project).
- **Projects & Code Files**: `project:<id>` from `ProjectRegistryService`, plus on-demand `file:<project_id>:<id>` (`contains`, `linked`) loaded progressively via `expandProject` from Graphify (`GET /api/v2/projects/{id}/graph`).
- **Agents, Workflows, Tasks, Tools, Skills, Models, Documents**:
  - `agent:<role>` `-[executes]->` `task:<run_id>`
  - `task:<run_id>` `-[part_of]->` `workflow:<workflow_name>`
  - `task:<run_id>` `-[runs_on]->` `project:<project_id>`
  - `task:<run_id>` / `conversation:current` `-[uses_tool]->` `tool:<name>` (real executed step/web/workflow tools only)
  - `skill:<skill_id>` `-[supports]->` `agent:<role>`, `task:<run_id>` `-[used_skill]->` `skill:<skill_id>`
  - `task:<run_id>` `-[used_model]->` `model:<provider>:<model_id>`
  - `task:<run_id>` `-[produced]->` `document:<name>`
- **Knowledge & Conversations**: `knowledge:<source_id>` and `conversation:current` / `conversation:<session_id>` (`references`, `belongs_to`, `uses_tool`).

Links are deduplicated by `(source, relation, target)` and validated so dangling edges referencing unprojected endpoints are discarded server-side.

## 2. Frontend Graph State & Incremental Store
- **Pure Core (`apps/frontend/src/graphCore.ts`)**:
  - `acceptSeq(graph, seq)`: rejects duplicate or stale out-of-order deltas (`seq <= lastSeq`).
  - `applyDelta(graph, delta)`: mutates existing node objects in place (`Object.assign`) so 3D simulation coordinates (`x, y, z, vx, vy, vz`) persist cleanly across live updates; cascades deletions across incident links.
  - `applySnapshot(graph, snap)`: diffs against server truth while preserving `local: true` client-expanded project code nodes.
  - `degreeMap(graph)` & `topHubs(graph, limit)`: computes real undirected degree centrality for Top Hubs ranking and node visual sizing.
- **Live Store (`apps/frontend/src/graphStore.ts`)**:
  - Coalesces bursty delta frames into 80 ms batches via `flush()`.
  - Maintains 8-second real activity highlight expirations (`conversation.updated`, `agent.started`, `agent.progress`, `skill.executed`).

## 3. WebGL Renderer & Physics Engine (`apps/frontend/src/components/GalaxyCanvas.tsx`)
- Uses vendored `ForceGraph3D` (`window.ForceGraph3D` + `window.THREE`) with additive cyan radial sprite textures (`getGlowTexture` cached by color + active state) on a deep `#03060c` canvas.
- Supports three real physics/layout modes (`force`, `cluster` type-centroid sphere, `radial` hierarchy shells), live parameter controls (`repulsion`, `linkDistance`, `nodeScale`, `speed`, `labelMode`, `edgeMode`, `frozen`), drag position pinning/unpinning, double-click camera framing, and right-click HUD context menu actions.

## 4. Security & Privacy Filtering
`sanitize_graph_text` filters descriptions, titles, and detail strings before serialization or SSE emission to prevent credential leakage (`sk-*`, `AIza*`, `ghp_*`, `hf_*`, `Bearer`, private keys, and `api_key`/`password`/`secret`/`token` values replaced with `[REDACTED]`). `FiveWayMemoryStore` continues rejecting raw secrets at the API boundary.

