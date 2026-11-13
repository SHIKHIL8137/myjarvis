# JARVIS MASTER IMPLEMENTATION ROADMAP (PHASES A–I)
**Date**: November 13, 2026  
**Target Completion**: Production Hardened JARVIS Assistant  
**Tracking Status**: Active Implementation  

---

## Roadmap Overview

```
Phase A: Audit & Baseline (Completed: 292 pytest passed, 12 frontend passed)
   |
   v
Phase B: Critical Correctness & Security Fixes
   - Remove OpenCode from Model Gateway provider parser
   - Sync Alembic version & database engine reconciliation
   - Prevent casual conversational questions from creating SQLite task records
   |
   v
Phase C: Architectural Consistency & Domain Separation
   - Implement RuntimeRegistry separating Providers, Models, Agent Applications, Runtimes, Profiles, and Assignments
   - Expose dynamic evidence-based runtime inventory (no fictional agent counts)
   |
   v
Phase D: Personal Assistant Foundation
   - Complete StructuredRequest intent planning & follow-up disambiguation
   - Ensure conversational continuity across active projects and active tasks
   |
   v
Phase E: Agent Ecosystem & Capability Verification
   - Safe executable discovery for OpenCode, Claude Code, and Antigravity
   - Evidence levels: not_discovered -> installed -> auth_verified -> capability_tested
   - Worktree isolation with pre-action diff security reviews
   |
   v
Phase F: Daily Assistant Workflows
   - Calendar availability checks & conflict detection
   - Email draft vs send approval separation
   - Automated Daily Morning Briefing & End-of-Day Review routines
   |
   v
Phase G: Knowledge & Work Capabilities
   - Autonomous Project Explorer with repository mapping & structural audit
   - PDF & document extraction with citation provenance
   |
   v
Phase H: UX, Voice & Operations
   - Live activity timeline event streaming in Operations Center
   - Immediate voice acknowledgment before long-running tasks
   - Multilingual transcription & language preservation
   |
   v
Phase I: Production Hardening & Verification
   - Threat model validation & PII firewall verification
   - 24 end-to-end acceptance scenarios
   - Production deployment & disaster recovery runbook
```

---

## Detailed Phase Breakdown

### Phase A — Audit and Baseline (Status: COMPLETED)
- Verified baseline test suite: 292 passed, 4 skipped, 0 failed.
- Frontend test suite: 12 passed, 0 failed; TypeScript: 0 errors.
- Documented findings in `docs/production/JARVIS_CURRENT_STATE_AUDIT.md`.

### Phase B — Critical Correctness and Security Fixes
- `jarvis/gateway/providers.py`: Clean up `parse_model_key` to remove `"opencode"` as a model provider.
- `jarvis/assistant.py`: Ensure direct chat turns do not call `tasks.create_task`.
- `jarvis/db/engine.py`: Ensure `alembic_version` is stamped automatically on database initialization.

### Phase C — Architectural Consistency (Provider vs Model vs Runtime vs Agent)
- Implement `jarvis/core/runtime_registry.py` with:
  - `ProviderOrg`
  - `ModelCatalogEntry`
  - `AgentApplication`
  - `RuntimeInstance`
  - `AgentInstance`
  - `AgentProfile`
  - `TaskAssignment`
- Expose `/api/v2/runtimes` with dynamically computed evidence levels.

### Phase D — Personal Assistant Foundation
- Wire `build_structured_request` into `jarvis/decision/router.py`.
- Thread `ConversationContext` through turn execution in `jarvis/assistant.py`.
- Handle task steering and follow-up project disambiguation.

### Phase E — Agent Ecosystem & Capability Verification
- Build `RuntimeDiscoveryService` probing `opencode`, `claude`, and `antigravity` safely without executing untrusted commands.
- Capability registry tracking verification method, confidence, and timestamp.

### Phase F — Daily Assistant Workflows
- Expand `LocalCalendarService` with `check_availability` and `detect_conflicts`.
- Expand `EmailDraftService` with search and draft prioritization.
- Add `daily_morning_briefing` and `end_of_day_review` routines honoring quiet hours.

### Phase G — Knowledge & Work Capabilities
- Implement `ProjectExplorerAgent` (`jarvis/agents/project_explorer.py`) for autonomous codebase indexing.
- Citations and document provenance in knowledge retrieval.

### Phase H — UX, Voice & Operations Center
- Update `apps/frontend/src/components/OperationsDrawer.tsx` with live timeline events and runtime cards.
- Add voice pre-task audio acknowledgment in `jarvis/assistant.py`.

### Phase I — Production Hardening & Acceptance
- Execute 24 acceptance scenarios.
- Run complete non-regression test suite.
- Deliver `docs/production/JARVIS_FINAL_VERIFICATION_REPORT.md`.

