# P0–P6 Comprehensive Test Plan & Verification Strategy

## Purpose & Scope

This document specifies the verification strategy for Phases P0 through P6 of the JARVIS assistant. Testing covers:
- Core contracts, error envelopes, and correlation context logging (**P0**).
- Durable task transitions, checkpoint persistence, budgets, pause/steer, and resumption (**P1**).
- Verbatim quote offset spans, provenance tracking, and contradiction detection (**P2**).
- Markdown profile discovery, deterministic tool filtering, and bounded delegation (**P3**).
- APScheduler routines, quiet hours overnight wrapping, and persistent dismissals (**P4**).
- Action execution verification (`ToolOutcome`), undo rollback, and conflict prevention (**P5**).
- React/TypeScript HUD Operations Center, SSE events, and end-to-end scenarios A–J (**P6**).

## Test Suite Inventory

| Test Module | Coverage Area | Assertions & Invariants |
|---|---|---|
| `tests/test_p0_foundation.py` | Contracts, Envelopes, Correlation | Status normalization, correlation ID in headers/logs, secret masking in logs, ErrorEnvelope structure |
| `tests/test_p1_background_tasks.py` | Background Task Engine & Migration 0009 | Canonical transitions, pause/resume, expired grant blocking, live steering notes, step budgets |
| `tests/test_p2_deep_research.py` | Evidence-Based Deep Research | `[start:end]` offset coordinates, provenance badges, SSRF blocking, local folder authorization |
| `tests/test_p3_agent_profiles.py` | Markdown Agent Profiles | Project > user > builtin precedence, tool filtering allow/deny, privilege escalation rejection |
| `tests/test_p4_routines_and_proactive.py` | Routines & Proactive Engine | Overnight quiet hours (22:00-07:00), cron recalculation, dismissal persistence surviving restart |
| `tests/test_p5_verified_actions_and_undo.py` | Action Verification & Undo | `ToolOutcome` checks, binary pre-action snapshots, disk modification conflict rejection |
| `tests/test_p6_operations_and_e2e.py` | Scenarios A through J End-to-End | Full integration of all 10 architectural scenarios |
| `apps/frontend/src/*.test.ts` | Frontend Client & State Store | Safe media enumeration, speech inspector states, graph topology deduplication |

## Quality Gates

1. **Zero Fake Passes**: All assertions must run against real code, genuine database models, and real or mock service instances.
2. **Deterministic Execution**: Tests must pass consistently offline without depending on third-party cloud APIs.
3. **Database Migration Roundtrip**: Every Alembic migration (`0009`, `0010`, `0011`) must upgrade and downgrade cleanly without orphaned schema artifacts.
4. **Static Analysis & Types**: Zero `mypy` or `ruff` errors allowed across `jarvis` and `tests`.
5. **Frontend Build Cleanliness**: `npm test` and `npm run build` must complete with 0 errors and 0 warnings.

