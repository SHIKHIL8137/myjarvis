# Unified Operations Center & HUD Integration (P6)

## Overview

The P6 Operations Center provides a centralized, 10-tab control panel embedded directly inside the JARVIS React/TypeScript Dark HUD (`apps/frontend/src/components/OperationsDrawer.tsx`).

## Operations Center Tabs

1. **Tasks**:
   - Filter background tasks by status (`all`, `active`, `scheduled`, `paused`, `done`, `failed`).
   - Pause, resume, and cancel buttons.
   - Live steering input box to inject instructions into in-flight tasks.
   - Displays step and token budget consumption counters and partial progress reports.

2. **Agents**:
   - Workforce profile registry loaded from project, user, and built-in scopes.
   - Shows privilege tier badges (`READ`, `PROMPT`, `EXECUTE`, `ADMIN`), delegation depth limits, allowed tools, and denylists.
   - One-click "Reload Profiles" button with real-time diagnostic reporting.

3. **Research**:
   - Evidence-based deep research report explorer.
   - Detailed claim view featuring verbatim quote offset spans (`[start:end]`) and provenance badges (`extracted_quote`, `synthesized_claim`, `inference`).
   - Flagged cross-source contradictions highlighted in alert boxes.
   - Freshness & limitations telemetry (scanned paths count, latest source timestamp, gaps).

4. **Routines**:
   - Scheduled routines manager with APScheduler cron, interval, and startup triggers.
   - Active quiet hours indicator badge.
   - Enable/disable toggles and one-click "Run Now" execution triggers.
   - Routine creation form and execution audit journal with idempotency keys.

5. **Actions & Undo**:
   - Undo Journal listing reversible file modifications with SHA-256 digests.
   - One-click "Revert / Undo" rollback button with instant conflict warning toasts.
   - Action Execution Audit Log displaying verified `ToolOutcome` contracts, security permission levels, and discrete checks.

6. **History**:
   - Searchable natural-language command execution log with one-click replay.

7. **Aliases**:
   - User-approved friendly project alias mappings to local directory paths (e.g. `ERP_V2` → `D:\Projects\ERP_V2`).

8. **Memory**:
   - Adaptive memory category toggles and conversational memory pruning.

9. **Desktop**:
   - Live window listing, window focus, media control, screenshot capture, and pending action approval gates.

10. **Sysinfo**:
    - Real-time CPU, RAM, GPU/VRAM, and battery hardware telemetry with active resource alerts.

## HUD Navigation & Link Integrity

All node actions in `apps/frontend/src/components/Inspector.tsx` route through `App.tsx`:
- Memory nodes route directly to the Memory drawer.
- Project nodes route directly to Aliases / Projects.
- Task and Agent nodes route directly to Tasks / Agents.
- Zero broken links, unhandled routes, or missing handlers exist in the application shell.

