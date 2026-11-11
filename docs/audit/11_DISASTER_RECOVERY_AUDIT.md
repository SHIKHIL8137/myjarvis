# 11 — Disaster Recovery & 3-2-1 Backup Architecture Audit

> **Current Reality**: Jarvis currently only has `PersonalMemoryStore.backup()` / `restore()` (raw unencrypted JSONL snapshot in `data/memory/backups/`), soft-delete `notes/.trash/`, and SQLite KB version rollback (`KnowledgePipeline.rollback_source()`).
> There is **no full-system backup**, **no encrypted secrets export**, **no backup integrity manifest**, **no PostgreSQL/vector snapshot pipeline**, and **no automated restore verification**.

---

## 1. Disaster Scenarios & Current Exposure

| Failure Event | Currently Survivable? | Data Lost Today |
| :--- | :--- | :--- |
| Accidental file/note delete | Partial (`notes/.trash/`) | Pending memory approvals, `TaskManager` tasks, recent STT transcripts (RAM-only) |
| Local disk / SSD failure | **NO** | `data/history.jsonl`, `data/memory/`, `data/secrets.json`, `data/settings.json`, `data/learning/*`, `notes/`, `logs/` |
| Bad schema/data migration | **NO** | Corrupted `index.db` or JSONL files without pre-migration transactional snapshot |
| Workstation replacement | Manual only | Requires manual install + re-downloading ~1.14 GB speech models, Ollama models, recreating secrets & settings |

---

## 2. Target 3-2-1 Backup Strategy

Implement `backend/recovery/` enforcing **3 copies**, across **2 distinct media types**, with **1 offsite/offline copy**:

### Tier 1 — Hot Recovery (On-Host Continuous, RPO ≤ 15m)
- **Database & Vectors**: Automated PostgreSQL base backup + WAL archiving (`pg_basebackup` / scheduled `pg_dump -Fc` covering relational tables + `pgvector` HNSW indexes atomically).
- **Config & Artifacts**: Hardlinked/copy snapshot of `config.json`, `data/settings.json`, `notes/`, and small `registry.json` metadata.
- **Trigger Hook**: Mandatory automatic pre-migration & pre-destructive-agent-action checkpoint (`Tier 1` snapshot created before any Alembic upgrade or bulk write).

### Tier 2 — Warm Local Vault (Secondary Drive / NAS, Daily Schedule)
- Consolidated timestamped bundle (`jarvis-backup-YYYYMMDD-HHMMSS/`):
  - `db.dump` (`pg_dump --format=custom`)
  - `notes_snapshot.tar.zst` (Markdown workspace + `notes/` export)
  - `learning_artifacts.tar.zst` (datasets builds, small LoRA adapters, eval reports — *excluding reproducible huge base models like Whisper/Ollama tags unless explicitly requested!*)
  - `secrets.vault.age` (encrypted secrets vault using passphrase/hardware key, **never plaintext `secrets.json`**!)
  - `manifest.json` containing SHA-256 checksums for every archive member + schema/app version (`alembic_rev`, `git_sha`) + size metadata.

### Tier 3 — Cold Offsite / Offline Copy (Weekly + Manual On-Demand)
- **Fully Offline Option (Zero Internet Needed)**: Export encrypted Tier-2 bundle onto encrypted USB SSD / external HDD (`verify-usb` command).
- **Optional Encrypted Cloud Offsite (Online Mode Only)**: Encrypted upload (e.g., Restic/rclone/S3-compatible client-side encrypted blob via allowlisted host) — ciphertext-only leaves host; encryption keys remain solely with the owner.

---

## 3. Secrets Encryption & Security Gap Fix

- **Existing Gap**: `jarvis/config.py::set_provider_api_key()` writes API keys cleanly outside `config.json`, into `data/secrets.json` — **in plaintext JSON** (`0600`-equivalent Windows user file permissions only).
- **Target Remediation (`P2–P3`)**:
  1. Runtime storage on Windows: encrypt provider API keys using **Windows DPAPI** (`CryptProtectData`) or OS Keyring, falling back cleanly only in test harnesses.
  2. Portable Backup Vault: during DR export, decrypt in-memory and re-encrypt using **AEAD (`cryptography` AES-256-GCM / XChaCha20 / Argon2id KDF)** with a user recovery passphrase into `secrets.vault.enc` so backups remain restorable on a brand-new machine without relying on old Windows machine DPAPI keys!

---

## 4. Automated Restore Drill & Replacement-Machine Bootstrap

1. **Automated Restore Verification (`jarvis-dr verify <bundle>`)**:
   - Verifies SHA-256 of every file in `manifest.json`.
   - Extracts into an **ephemeral verification sandbox** (isolated temp Postgres schema or container / temp dir), runs `pg_restore --list` / test load + Alembic schema check, validates row counts match `manifest.json`, verifies secret vault decryptability, and records `"verified_restore = true"` in `backup_manifests`.
2. **Replacement-Laptop Bootstrap Script (`scripts/bootstrap.ps1`)**:
   - Step 1: Verify Python `3.12+` / `3.14+`, PostgreSQL + `pgvector`, Node `20+`, Git, Ollama, OpenCode, Graphify.
   - Step 2: Create `.venv`, install pinned deps, restore Ollama pulls (`qwen3:4b-instruct`, etc.) + Whisper/Piper/Wake weights cleanly (`scripts/fetch_models.py`).
   - Step 3: Run `jarvis-dr restore <bundle> --verify` → populates DB, vectors, encrypted secrets vault, notes, and learning state → executes `preflight.py` + `pytest` smoke test before going live.
