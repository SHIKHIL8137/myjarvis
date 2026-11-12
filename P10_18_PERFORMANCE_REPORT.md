# JARVIS P10.18 — Performance & Scaling Report

## 1. Benchmark Scope & Isolation
All synthetic scaling tests were executed in isolation (`scratch/bench_graph_core.mjs` for Node client store logic and `scratch/bench_graph_server.py` + `scratch/bench_backend_graph.py` on port `4799` for HTTP serialization) without altering or injecting synthetic items into the real Jarvis database (`data/`) or `/api/v2/graph`.

## 2. Isolated Synthetic Graph Measurements (`100` to `5,000` Nodes)

| Synthetic Nodes | Synthetic Edges | `applySnapshot` Merge (ms) | `degreeMap` + `topHubs` (ms) | 50-Node Delta Merge (ms) | HTTP JSON Avg Latency (ms) | Payload Size (KB) | Node JS Heap (MB) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 189 | 0.675 | 8.967* | 0.082 | 2.27 | 33.4 | 8.58 |
| 500 | 987 | 2.135 | 1.356 | 0.116 | 12.98 | 171.6 | 10.01 |
| 1,000 | 1,987 | 3.658 | 1.769 | 0.145 | 9.82 | 344.5 | 11.31 |
| 5,000 | 9,987 | 14.980 | 10.636 | 0.094 | 43.66 | 1751.4 | 17.77 |

\* Includes initial V8 JIT warmup on the first iteration; warm centrality pass at 500–1,000 nodes completes in 1.36–1.77 ms.

## 3. Live Production Endpoint Latency (`127.0.0.1:4700`)
- Live unified snapshot `GET /api/v2/graph?limit=300` (`44` real nodes, `85` real edges): **29.7 ms** (`seq=26`).
- Live `/api/events` SSE event propagation: **21.39 ms** (`memory.created`) and **16.25 ms** (`memory.deleted`).
- Frontend bundle build (`vite build`): `52` modules transformed in **1.01 s** (`index-*.js` gzip `68.17 kB`, lazy module chunks `1.00–3.67 kB`).

## 4. Renderer Optimization Techniques Applied
- In-place node/link identity reuse (`rn` and `rl` maps in `GalaxyCanvas.tsx`) avoids re-creating Three.js nodes or resetting force coordinates on delta updates.
- Radial glow sprite textures are cached per `(color, highlighted)` pair (`glowTextures` Map) and shared across `SpriteMaterial` instances.
- Smart label mode avoids allocating hundreds of text canvas textures simultaneously at scale (rendering labels by default on active/selected/neighbor/hub nodes or when `Labels: all` is explicitly chosen).
- SSE event bursts coalesce within an 80 ms batch window (`flush()` in `graphStore.ts`) so high-frequency updates trigger a single store bump.

## 5. Measurement Gap (In-Browser WebGL FPS & JS Heap)
Live Chromium WebGL FPS and `performance.memory` sampling via `chrome_devtools` MCP was not completed in this run because Google Chrome remote debugging (`chrome://inspect/#remote-debugging`, `DevToolsActivePort`) was unavailable on the host browser session (`ENOENT`). Client store merge/centrality/HTTP latencies above were verified deterministically.

