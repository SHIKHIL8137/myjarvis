# System Monitoring & Proactive Suggestions Report (Stage E)

## Summary
Jarvis now monitors CPU, RAM, GPU/VRAM/temperature, fixed storage drives, battery status, local network interfaces, top processes, and Jarvis runtime queues with honest sensor reporting, configurable threshold alerts, bounded history, and strictly non-executing proactive HUD suggestions.

## Implemented Capabilities
- **Honest Local Telemetry (`jarvis/sysinfo.py`, `jarvis/config.py`)**:
  - Resolved all 3 pre-existing `mypy` errors in `jarvis/sysinfo.py`.
  - `cpu()` and `cpu_details()` measure logical core utilization and explicitly report `temp_c: None`, `temp_supported: False`, and an `unavailable_reason` on user-mode Windows rather than fabricating thermal values.
  - `ram()` reads physical memory load/free/used GB via `GlobalMemoryStatusEx`.
  - `gpu()` queries `nvidia-smi` (3-second cache) including `temperature.gpu` alongside total/used/free VRAM and utilization. Observed on local host: `NVIDIA GeForce GTX 1650`, `4.0 GB` total, `51.0°C`.
  - `disks()`, `battery()`, `network()`, and `processes(limit)` enumerate fixed drives (`shutil.disk_usage`), Windows `GetSystemPowerStatus`, non-loopback local addresses without external traffic, and top memory-sorted processes via `tasklist /FO CSV /NH`.
  - `evaluate_alerts(snap, spec)` checks real thresholds defined in `MonitoringSpec` (`cpu_alert_pct`, `ram_alert_pct`, `gpu_vram_alert_pct`, `disk_free_alert_gb`, `battery_low_pct`); `SystemMonitorHistory` retains a bounded snapshot ring buffer.
- **Opt-In Non-Executing Proactive Engine (`jarvis/proactive.py`, `jarvis/config.py`, `jarvis/assistant.py`)**:
  - `ProactiveSuggestionEngine` (`ProactiveSpec`) generates dismissible cards from real resource alerts, failed/paused tasks, and repeated commands (`>=3x`) while enforcing `auto_execute=False` and `requires_user_action=True` on every card, with configurable cooldown suppression.
- **APIs, Intents & HUD (`jarvis/api.py`, `jarvis/api_v2.py`, `jarvis/intent.py`, `apps/frontend/src/components/OperationsDrawer.tsx`, `apps/frontend/src/features/AssistantHome.tsx`)**:
  - Added `/api/sysinfo`, `/api/v2/system/health`, `/api/v2/system/suggestions`, and `/api/v2/system/suggestions/{suggestion_id}/dismiss`, plus NL intents (`SYSTEM_HEALTH_CMD`, `PROACTIVE_CMD`), Sysinfo drawer cards, and non-executing proactive suggestion banners in `AssistantHome`.

## Verification & Measured Latency
- Verified via `tests/test_stage_e_sysinfo_proactive.py`.
- Measured uncached `sysinfo.snapshot(include_processes=False)` latency: `512.72 ms`; deterministic NL `system_health` chat execution latency: `37.15 ms`.

